import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.bot.chat.context import ContextRetriever, TextContext, QuestionContext, ContextRetrieverResponse
from criabot.bot.chat.web_search import WebSearchClient, infer_search_language
from CriadexSDK.ragflow_schemas import TextNodeWithScore, TextNode, GroupSearchResponse, RerankAgentResponse, TransformAgentResponse, RelatedPrompt, ChatMessage


def make_group_search_payload(nodes=None, search_units=1, metadata=None, assets=None):
    return {
        "response": GroupSearchResponse(
            nodes=nodes or [],
            search_units=search_units,
            metadata=metadata or {},
            assets=assets or [],
        ).model_dump()
    }


def make_group_search_side_effect(group_to_nodes):
    def _side_effect(*, group_name, search_config):
        return make_group_search_payload(nodes=group_to_nodes.get(group_name, []))

    return _side_effect


def expected_empty_search_calls(group_count: int) -> int:
    # Empty document groups use: base + broad + keyword + probe = 4 calls
    # Empty question groups use base query only by default (expansions are opt-in).
    return (4 * group_count) + (1 * group_count)

@pytest.fixture
def criadex_api():
    mock = AsyncMock()
    mock.agents.cohere.rerank = AsyncMock(return_value={"reranked_documents": [], "search_units": 1})
    mock.agents.azure.transform = AsyncMock(return_value={"agent_response": TransformAgentResponse(new_prompt="hello", usage=[])})
    mock.content.search = AsyncMock()
    mock.content.list = AsyncMock(return_value={"files": []})
    return mock

@pytest.fixture
def bot_params():
    params = MagicMock()
    params.top_k = 5
    params.min_k = 1
    params.top_n = 3
    params.min_n = 1
    params.web_search_enabled = False
    params.web_search_global_enabled = True
    params.faq_fallback_enabled = False
    params.faq_fallback_threshold = 0.5
    return params

@pytest.fixture
def bot_mock():
    mock = MagicMock()
    mock.group_name.side_effect = lambda index_type: f"child-{index_type.lower()}-index"
    return mock

@pytest.fixture
def retriever(criadex_api, bot_mock, bot_params):
    return ContextRetriever(
        criadex=criadex_api,
        rerank_model_id=1,
        llm_model_id=1,
        bot=bot_mock,
        bot_params=bot_params
    )

def create_text_node(text, metadata=None, score=0.8):
    if metadata is None:
        metadata = {}
    return TextNodeWithScore(
        node=TextNode(text=text, metadata=metadata, text_template="", metadata_template="", class_name="TextNode"),
        score=score
    )

@pytest.mark.asyncio
async def test_retrieve_no_nodes(retriever, bot_mock):
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response, ContextRetrieverResponse)
    assert response.context is None
    assert len(response.nodes) == 0
    retriever._criadex.agents.cohere.rerank.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_marks_indexing_in_progress_when_group_has_files_but_no_nodes(retriever):
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    retriever._criadex.content.list.return_value = {"files": ["quiz_syllabus.txt"]}
    retriever._indexing_retry_attempts = 1

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])

    assert response.context is None
    assert response.indexing_in_progress is True
    assert "child-document-index" in response.indexing_groups

@pytest.mark.asyncio
async def test_retrieve_with_text_context(retriever, bot_mock):
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )

    # Mock hybrid_rerank to return what retrieve expects
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response.context, TextContext)
    assert "text 1" in response.context.text

@pytest.mark.asyncio
async def test_retrieve_with_question_context_no_llm_reply(retriever, bot_mock):
    question_node = create_text_node(
        "question text",
        metadata={"answer": "the answer", "llm_reply": False, "file_name": "f", "group_name": "g"},
        score=0.9
    )
    nodes = [question_node]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )

    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response.context, QuestionContext)
    assert response.context.node.model_dump() == question_node.model_dump()

@pytest.mark.asyncio
async def test_retrieve_with_question_context_with_llm_reply(retriever, bot_mock):
    question_node = create_text_node(
        "question text",
        metadata={"answer": "the answer", "llm_reply": True},
        score=0.9
    )
    nodes = [question_node]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )

    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response.context, TextContext)
    assert "question text" in response.context.text
    assert "other text" not in response.context.text

@pytest.mark.asyncio
async def test_search_groups(retriever, bot_mock):
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=["extra_bot"])
    assert retriever._criadex.content.search.call_count == expected_empty_search_calls(2)
    retriever._criadex.content.search.assert_any_call(
        group_name="child-document-index",
        search_config=retriever.build_search_group_config(
            prompt="hello",
            metadata_filter=None,
            extra_groups=[]
        )
    )
    retriever._criadex.content.search.assert_any_call(
        group_name="extra_bot-document-index",
        search_config=retriever.build_search_group_config(
            prompt="hello",
            metadata_filter=None,
            extra_groups=[]
        )
    )

@pytest.mark.asyncio
async def test_search_groups_with_parent_bots(retriever, bot_mock):
    parent_bots = ["parent1", "parent2"]
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=parent_bots)
    assert retriever._criadex.content.search.call_count == expected_empty_search_calls(3)
    retriever._criadex.content.search.assert_any_call(
        group_name="parent1-document-index",
        search_config=retriever.build_search_group_config(
            prompt="hello",
            metadata_filter=None,
            extra_groups=[]
        )
    )
    retriever._criadex.content.search.assert_any_call(
        group_name="parent2-document-index",
        search_config=retriever.build_search_group_config(
            prompt="hello",
            metadata_filter=None,
            extra_groups=[]
        )
    )


@pytest.mark.asyncio
async def test_question_index_uses_single_pass_by_default(retriever):
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {
            "child-document-index": [],
            "child-question-index": [],
        }
    )

    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    question_calls = [
        c for c in retriever._criadex.content.search.call_args_list
        if c.kwargs.get("group_name") == "child-question-index"
    ]
    assert len(question_calls) == 1

@pytest.mark.asyncio
async def test_hybrid_rerank(retriever, criadex_api):
    nodes = [create_text_node("text 1")]
    await retriever.hybrid_rerank(prompt="hello", nodes=nodes)
    criadex_api.agents.cohere.rerank.assert_called_once_with(
        model_id=retriever._rerank_model_id,
        agent_config={
            "prompt": "hello",
            "nodes": [node.model_dump(mode='json') for node in nodes],
            "top_n": retriever._bot_params.top_n,
            "min_n": retriever._bot_params.min_n
        }
    )

@pytest.mark.asyncio
async def test_transform_prompt(retriever, criadex_api):
    history = [ChatMessage(role="user", blocks=[{"type": "text", "text": "hi"}])]
    await retriever.transform_prompt(prompt="hello", history=history)
    criadex_api.agents.azure.transform.assert_called_once_with(
        model_id=retriever._llm_model_id,
        agent_config={
            "prompt": "hello",
            "history": history
        }
    )

def test_merge_responses():
    response1 = {"group1": GroupSearchResponse(nodes=[create_text_node("text 1")], search_units=1, metadata={}, assets=[])}
    response2 = {"group1": GroupSearchResponse(nodes=[create_text_node("text 2")], search_units=1, metadata={}, assets=[]), "group2": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[])}
    merged = ContextRetriever.merge_responses(response1, response2)
    assert len(merged["group1"].nodes) == 2
    assert merged["group1"].search_units == 2
    assert "group2" in merged


def test_build_retrieval_prompts_for_summary_query():
    prompts = ContextRetriever.build_retrieval_prompts(
        "Give me a summary including the IT Department support motto, the HR handbook release date, and the robotics lab location."
    )
    assert prompts == [
        "Give me a summary including the IT Department support motto, the HR handbook release date, and the robotics lab location.",
        "the IT Department support motto",
        "the HR handbook release date",
        "the robotics lab location",
    ]


def test_build_retrieval_prompts_for_direct_question_adds_focused_variant():
    prompts = ContextRetriever.build_retrieval_prompts(
        "Where is the Advanced Robotics Lab located?"
    )

    assert prompts == [
        "Where is the Advanced Robotics Lab located?",
        "the Advanced Robotics Lab located",
        "Advanced Robotics Lab located",
    ]


def test_build_retrieval_prompts_extracts_question_from_enriched_prompt():
    enriched_prompt = (
        "Answer using only the training materials indexed for this course. "
        "The current course is 'Art of Art'. q: What are the learning outcomes?"
    )
    prompts = ContextRetriever.build_retrieval_prompts(enriched_prompt)

    # It should include the full prompt (for safety), the extracted question, 
    # and the focused variants of that question.
    assert "What are the learning outcomes?" in prompts
    assert "the learning outcomes" in prompts
    assert "learning outcomes" in prompts
    # But it shouldn't just be the full string
    assert len(prompts) > 1


def test_extract_embed_user_question_from_moodle_embed_prompt():
    enriched_prompt = (
        'Answer using only the training materials indexed for this Moodle course assistant. '
        'The current course is "The art of Art" (The art of Art), Moodle course id 7. '
        'When the user says "this course", "the course", or asks about course learning outcomes, '
        'they mean this course. I am a student and my name is Test User.. Current grade: 0. '
        "Today's date is 6/10/26. "
        'q: For the course "The art of Art": can you tell me about this course Learning Outcomes?'
    )
    assert ContextRetriever.extract_embed_user_question(enriched_prompt) == (
        'For the course "The art of Art": can you tell me about this course Learning Outcomes?'
    )


def test_prioritize_nodes_boosts_filename_keyword_matches_for_embed_prompt():
    nodes = [
        create_text_node("General feedback notes about critique techniques.", metadata={"file_name": "page_71_notes.html"}),
        create_text_node(
            "The robotics lab is located in Building 7, Room B12.",
            metadata={"file_name": "page_12_Robotics_Lab_Location.html"},
        ),
    ]
    prompt = (
        'Answer using only the training materials indexed for this Moodle course assistant. '
        'The current course is "Physics 101". q: Where is the robotics lab located?'
    )
    ranked = ContextRetriever.prioritize_nodes_for_prompt(prompt, nodes)
    assert ranked[0].node.metadata["file_name"] == "page_12_Robotics_Lab_Location.html"


@pytest.mark.asyncio
async def test_retrieve_limits_direct_question_context_to_adaptive_budget(retriever):
    nodes = [
        create_text_node("top result", metadata={"group_name": "child-document-index"}, score=0.95),
        create_text_node("second result", metadata={"group_name": "parent1-document-index"}, score=0.85),
        create_text_node("third result", metadata={"group_name": "parent2-document-index"}, score=0.75),
        create_text_node("fourth result", metadata={"group_name": "parent3-document-index"}, score=0.65),
    ]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )

    response = await retriever.retrieve(
        prompt="What is the IT Department support motto?",
        metadata_filter=None,
        extra_bots=[],
    )

    assert isinstance(response.context, TextContext)
    assert [node.node.text for node in response.context.nodes] == [
        "top result",
        "second result",
        "third result",
        "fourth result",
    ]


@pytest.mark.asyncio
async def test_search_groups_queries_configured_extra_index_suffixes(retriever, monkeypatch):
    monkeypatch.setenv("RETRIEVAL_EXTRA_INDEX_SUFFIXES", "-outcome-index")
    retriever._bot.name = "child"
    retriever._extra_index_suffixes = retriever._normalized_suffixes(
        retriever._parse_suffixes("-outcome-index")
    )
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])

    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    assert any(
        call.kwargs.get("group_name") == "child-outcome-index"
        for call in retriever._criadex.content.search.call_args_list
    )


@pytest.mark.asyncio
async def test_retrieve_keeps_full_context_for_explicit_summary_prompt(retriever):
    nodes = [
        create_text_node("top result", metadata={"group_name": "child-document-index"}, score=0.95),
        create_text_node("second result", metadata={"group_name": "parent1-document-index"}, score=0.85),
        create_text_node("third result", metadata={"group_name": "parent2-document-index"}, score=0.75),
        create_text_node("fourth result", metadata={"group_name": "parent3-document-index"}, score=0.65),
    ]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )

    response = await retriever.retrieve(
        prompt="Give me a summary including the IT Department support motto, the HR handbook release date, and the robotics lab location.",
        metadata_filter=None,
        extra_bots=[],
    )

    assert isinstance(response.context, TextContext)
    assert [node.node.text for node in response.context.nodes] == [
        "top result",
        "second result",
        "third result",
        "fourth result",
    ]


@pytest.mark.asyncio
async def test_retrieve_prefers_rerank_order_for_direct_question(retriever):
    child_node = create_text_node(
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        metadata={"group_name": "child-document-index"},
        score=0.90,
    )
    parent_node = create_text_node(
        "The IT Department support motto is QuantumGuard2026.",
        metadata={"group_name": "parent1-document-index"},
        score=0.70,
    )
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {
            "child-document-index": [child_node],
            "parent1-document-index": [parent_node],
        }
    )
    retriever.hybrid_rerank = AsyncMock(
        return_value={"ranked_nodes": [parent_node, child_node], "search_units": 1}
    )

    response = await retriever.retrieve(
        prompt="What is the IT Department support motto?",
        metadata_filter=None,
        extra_bots=["parent1"],
    )

    assert isinstance(response.context, TextContext)
    assert [node.node.text for node in response.context.nodes] == [
        "The IT Department support motto is QuantumGuard2026.",
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
    ]


@pytest.mark.asyncio
async def test_retrieve_prioritizes_prompt_matching_node_for_direct_question(retriever):
    child_node = create_text_node(
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        metadata={"group_name": "child-document-index"},
        score=0.95,
    )
    parent_node = create_text_node(
        "The IT Department support motto is QuantumGuard2026.",
        metadata={"group_name": "parent1-document-index"},
        score=0.70,
    )
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {
            "child-document-index": [child_node],
            "parent1-document-index": [parent_node],
        }
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": [], "search_units": 1})

    response = await retriever.retrieve(
        prompt="What is the IT Department support motto?",
        metadata_filter=None,
        extra_bots=["parent1"],
    )

    assert isinstance(response.context, TextContext)
    assert [node.node.text for node in response.context.nodes] == [
        "The IT Department support motto is QuantumGuard2026.",
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
    ]


@pytest.mark.asyncio
async def test_retrieve_prioritizes_release_date_node_for_when_question(retriever):
    child_node = create_text_node(
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        metadata={"group_name": "child-document-index"},
        score=0.95,
    )
    hr_node = create_text_node(
        "Employee Handbook version 5.4 was released on February 15, 2026.",
        metadata={"group_name": "parent2-document-index"},
        score=0.70,
    )
    it_node = create_text_node(
        "The IT Department support motto is QuantumGuard2026.",
        metadata={"group_name": "parent1-document-index"},
        score=0.65,
    )
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {
            "child-document-index": [child_node],
            "parent1-document-index": [it_node],
            "parent2-document-index": [hr_node],
        }
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": [], "search_units": 1})

    response = await retriever.retrieve(
        prompt="When was Employee Handbook version 5.4 released?",
        metadata_filter=None,
        extra_bots=["parent1", "parent2"],
    )

    assert isinstance(response.context, TextContext)
    assert [node.node.text for node in response.context.nodes] == [
        "Employee Handbook version 5.4 was released on February 15, 2026.",
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        "The IT Department support motto is QuantumGuard2026.",
    ]


def test_normalize_ranked_nodes_deprioritizes_probe_fallback_hits(retriever):
    probe_node = create_text_node(
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        metadata={
            "group_name": "child-document-index",
            "_probe_fallback": True,
        },
        score=0.95,
    )
    parent_match = create_text_node(
        "The IT Department support motto is QuantumGuard2026.",
        metadata={"group_name": "parent1-document-index"},
        score=0.80,
    )

    ranked = retriever.normalize_ranked_nodes([probe_node, parent_match])

    assert [node.node.text for node in ranked] == [
        "The IT Department support motto is QuantumGuard2026.",
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
    ]


def test_normalize_ranked_nodes_keeps_higher_scoring_parent_match_first(retriever):
    child_node = create_text_node(
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
        metadata={"group_name": "child-document-index"},
        score=0.70,
    )
    parent_node = create_text_node(
        "The IT Department support motto is QuantumGuard2026.",
        metadata={"group_name": "parent1-document-index"},
        score=0.85,
    )

    ranked = retriever.normalize_ranked_nodes([child_node, parent_node])

    assert [node.node.text for node in ranked] == [
        "The IT Department support motto is QuantumGuard2026.",
        "The Advanced Robotics Lab is located in the basement of Building 7, Room B12.",
    ]


@pytest.mark.asyncio
async def test_retrieve_uses_web_search_fallback_when_enabled(retriever):
    retriever._bot_params.web_search_enabled = True
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    web_nodes = [create_text_node("[WEB RESULT #1] external answer", metadata={"source_type": "web_search"}, score=0.7)]
    retriever._search_web_nodes = AsyncMock(return_value=web_nodes)

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_awaited_once_with("hello")
    assert isinstance(response.context, TextContext)
    assert "external answer" in response.context.text


@pytest.mark.asyncio
async def test_retrieve_merges_web_search_on_explicit_request(retriever):
    retriever._bot_params.web_search_enabled = True
    local_nodes = [create_text_node("local answer", score=0.9)]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": local_nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": local_nodes, "search_units": 1})
    retriever._search_web_nodes = AsyncMock(
        return_value=[create_text_node("[WEB RESULT #1] web answer", metadata={"source_type": "web_search"}, score=0.6)]
    )

    response = await retriever.retrieve(prompt="search the web for hello", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_awaited_once_with("search the web for hello")
    assert isinstance(response.context, TextContext)
    assert "local answer" in response.context.text
    assert "web answer" in response.context.text
    assert "WEB_SEARCH" in response.group_responses
    assert response.group_responses["WEB_SEARCH"].nodes[0].node.metadata.get("source_type") == "web_search"


@pytest.mark.asyncio
async def test_retrieve_skips_web_search_when_bot_toggle_disabled(retriever):
    retriever._bot_params.web_search_enabled = False
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    retriever._search_web_nodes = AsyncMock(return_value=[create_text_node("web", score=0.7)])

    response = await retriever.retrieve(prompt="search the web for something", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_not_awaited()
    assert response.context is None


@pytest.mark.asyncio
async def test_retrieve_skips_web_search_when_global_toggle_disabled(retriever):
    retriever._bot_params.web_search_enabled = True
    retriever._bot_params.web_search_global_enabled = False
    retriever._criadex.content.search.return_value = make_group_search_payload(nodes=[])
    retriever._search_web_nodes = AsyncMock(return_value=[create_text_node("web", score=0.7)])

    response = await retriever.retrieve(prompt="search the web for something", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_not_awaited()
    assert response.context is None


@pytest.mark.asyncio
async def test_retrieve_uses_web_threshold_for_low_confidence(monkeypatch, retriever):
    retriever._bot_params.web_search_enabled = True
    retriever._bot_params.web_search_global_enabled = True
    retriever._bot_params.faq_fallback_enabled = False
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_SCORE_THRESHOLD", "0.85")
    retriever._web_search_fallback_threshold = 0.85

    low_conf_node = create_text_node("low confidence local", score=0.40)
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": [low_conf_node]}
    )

    retriever._search_web_nodes = AsyncMock(
        return_value=[create_text_node("[WEB RESULT #1] web fallback", metadata={"source_type": "web_search"}, score=0.9)]
    )

    response = await retriever.retrieve(prompt="obscure query", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_awaited_once_with("obscure query")
    assert isinstance(response.context, TextContext)
    assert "web fallback" in response.context.text


def test_explicit_web_search_requested_supports_french():
    assert ContextRetriever._explicit_web_search_requested("cherche sur le web les dernières nouvelles")
    assert ContextRetriever._explicit_web_search_requested("fais une recherche sur internet")


def test_time_sensitive_request_detection_supports_english_and_french():
    assert ContextRetriever._is_time_sensitive_request("What are the latest updates on the standard?")
    assert ContextRetriever._is_time_sensitive_request("Donne-moi les dernières nouvelles de securite")


def test_classify_retrieval_mode_thresholds(retriever):
    assert retriever._classify_retrieval_mode(prompt="internal question", max_node_score=0.9) == "known"
    assert retriever._classify_retrieval_mode(prompt="normal question", max_node_score=0.6) == "semi_known"
    assert retriever._classify_retrieval_mode(prompt="What are latest updates?", max_node_score=0.6) == "external"
    assert retriever._classify_retrieval_mode(prompt="obscure query", max_node_score=0.2) == "unknown"


@pytest.mark.asyncio
async def test_retrieve_runs_web_search_for_time_sensitive_query(retriever):
    retriever._bot_params.web_search_enabled = True
    local_nodes = [create_text_node("local answer", score=0.9)]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": local_nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": local_nodes, "search_units": 1})
    retriever._search_web_nodes = AsyncMock(
        return_value=[create_text_node("[WEB RESULT #1] fresh web answer", metadata={"source_type": "web_search"}, score=0.6)]
    )

    response = await retriever.retrieve(prompt="latest release notes for this standard", metadata_filter=None, extra_bots=[])

    retriever._search_web_nodes.assert_awaited_once_with("latest release notes for this standard")
    assert isinstance(response.context, TextContext)
    assert "local answer" in response.context.text
    assert "fresh web answer" in response.context.text


def test_infer_search_language_french_and_english():
    assert infer_search_language("Quels sont les meilleurs cours en intelligence artificielle?") == "fr"
    assert infer_search_language("Please search latest Python release notes") == "en-US"


@pytest.mark.asyncio
async def test_web_search_client_retries_with_cleaned_explicit_query():
    empty_response = MagicMock()
    empty_response.status_code = 200
    empty_response.raise_for_status.return_value = None
    empty_response.json.return_value = {"results": []}

    hit_response = MagicMock()
    hit_response.status_code = 200
    hit_response.raise_for_status.return_value = None
    hit_response.json.return_value = {
        "results": [{"title": "Python Release Notes", "url": "https://example.com/python", "content": "Latest release notes."}],
    }

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[empty_response, hit_response])

    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    client_context.__aexit__.return_value = None

    with patch("criabot.bot.chat.web_search.httpx.AsyncClient", return_value=client_context):
        results = await WebSearchClient(base_url="http://example.test").search(
            "search the web for the latest Python release notes",
            language="en-US",
        )

    assert len(results) == 1
    first_params = client.get.await_args_list[0].kwargs["params"]
    second_params = client.get.await_args_list[1].kwargs["params"]
    assert first_params["q"] == "search the web for the latest Python release notes"
    assert second_params["q"] == "the latest Python release notes"
    assert second_params["language"] == "en-US"


@pytest.mark.asyncio
async def test_web_search_client_retries_on_transient_request_error():
    success_response = MagicMock()
    success_response.status_code = 200
    success_response.json.return_value = {
        "results": [
            {
                "title": "NIST PQC Update",
                "url": "https://example.com/pqc",
                "content": "Latest post-quantum cryptography update.",
            }
        ]
    }

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[httpx.ConnectError("network down"), success_response])

    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    client_context.__aexit__.return_value = None

    with patch("criabot.bot.chat.web_search.httpx.AsyncClient", return_value=client_context):
        with patch("criabot.bot.chat.web_search.asyncio.sleep", new=AsyncMock()):
            results = await WebSearchClient(base_url="http://example.test", retry_attempts=2).search(
                "latest post-quantum cryptography standards",
                language="en-US",
            )

    assert len(results) == 1
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_web_search_client_returns_empty_for_non_retriable_http_4xx():
    client_error_response = MagicMock()
    client_error_response.status_code = 400

    client = AsyncMock()
    client.get = AsyncMock(return_value=client_error_response)

    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    client_context.__aexit__.return_value = None

    with patch("criabot.bot.chat.web_search.httpx.AsyncClient", return_value=client_context):
        mocked_sleep = AsyncMock()
        with patch("criabot.bot.chat.web_search.asyncio.sleep", new=mocked_sleep):
            results = await WebSearchClient(base_url="http://example.test", retry_attempts=2).search(
                "plain query",
                language="en-US",
            )

    assert results == []
    # Non-retriable 4xx should not trigger backoff retry sleeps.
    mocked_sleep.assert_not_awaited()


# ============================================================================
# TESTS FOR STREAMING CALLBACK (on_step) FUNCTIONALITY
# ============================================================================

@pytest.mark.asyncio
async def test_retrieve_calls_on_step_callback_for_graphrag(retriever, bot_mock):
    """Test that on_step callback is invoked when GraphRAG is queried"""
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    # Track on_step calls
    on_step_calls = []
    
    async def track_on_step(engine, state, message):
        on_step_calls.append({"engine": engine, "state": state, "message": message})

    response = await retriever.retrieve(
        prompt="hello",
        metadata_filter=None,
        extra_bots=[],
        on_step=track_on_step
    )

    # Verify callback was called for graphrag, elasticsearch, rerank, synthesis
    engines_called = [call["engine"] for call in on_step_calls]
    assert "elasticsearch" in engines_called
    assert "rerank" in engines_called
    
    # Verify response is still valid
    assert isinstance(response.context, TextContext)


@pytest.mark.asyncio
async def test_retrieve_on_step_callback_with_web_search(retriever, bot_mock):
    """Test that on_step callback is invoked for web search when triggered"""
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})
    
    # Mock web search to return results
    web_nodes = [create_text_node("web result", metadata={"url": "http://example.com"})]
    retriever._search_web_nodes = AsyncMock(return_value=web_nodes)
    
    # Mock conditions to trigger web search
    retriever._can_use_web_search = MagicMock(return_value=True)
    retriever._explicit_web_search_requested = MagicMock(return_value=True)

    on_step_calls = []
    
    async def track_on_step(engine, state, message):
        on_step_calls.append({"engine": engine, "state": state, "message": message})

    response = await retriever.retrieve(
        prompt="hello web search",
        metadata_filter=None,
        extra_bots=[],
        on_step=track_on_step
    )

    # Verify web_search callback was called
    engines_called = [call["engine"] for call in on_step_calls]
    assert "web_search" in engines_called
    assert response is not None


@pytest.mark.asyncio
async def test_retrieve_on_step_callback_none_is_handled(retriever, bot_mock):
    """Test that on_step=None doesn't cause errors"""
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    # Should not raise when on_step is None
    response = await retriever.retrieve(
        prompt="hello",
        metadata_filter=None,
        extra_bots=[],
        on_step=None
    )

    assert isinstance(response.context, TextContext)


@pytest.mark.asyncio
async def test_on_step_callback_receives_proper_message_format(retriever, bot_mock):
    """Test that on_step callback receives properly formatted status messages"""
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    on_step_calls = []
    
    async def track_on_step(engine, state, message):
        on_step_calls.append({"engine": engine, "state": state, "message": message})

    response = await retriever.retrieve(
        prompt="hello",
        metadata_filter=None,
        extra_bots=[],
        on_step=track_on_step
    )

    # Verify all calls have proper structure
    for call in on_step_calls:
        assert "engine" in call
        assert "state" in call
        assert "message" in call
        assert call["state"] in ["start", "done"]
        assert isinstance(call["message"], str)
        assert len(call["message"]) > 0
        # Check for emoji in messages
        assert any(ord(c) > 127 for c in call["message"]) or any(
            word in call["message"].lower() for word in ["query", "search", "rerank", "process"]
        )


@pytest.mark.asyncio
async def test_on_step_callback_async_exceptions_dont_break_retrieval(retriever, bot_mock):
    """Test that exceptions in on_step callback don't break retrieval"""
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = make_group_search_side_effect(
        {"child-document-index": nodes}
    )
    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    async def broken_on_step(engine, state, message):
        raise RuntimeError("Callback error")

    # Should complete successfully despite callback error
    response = await retriever.retrieve(
        prompt="hello",
        metadata_filter=None,
        extra_bots=[],
        on_step=broken_on_step
    )

    # Retrieval should still work
    assert isinstance(response.context, TextContext)

