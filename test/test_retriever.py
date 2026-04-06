import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.bot.chat.context import ContextRetriever, TextContext, QuestionContext, ContextRetrieverResponse
from CriadexSDK.ragflow_schemas import TextNodeWithScore, TextNode, GroupSearchResponse, RerankAgentResponse, TransformAgentResponse, RelatedPrompt, ChatMessage

@pytest.fixture
def criadex_api():
    mock = AsyncMock()
    mock.agents.cohere.rerank = AsyncMock(return_value={"reranked_documents": [], "search_units": 1})
    mock.agents.azure.transform = AsyncMock(return_value={"agent_response": TransformAgentResponse(new_prompt="hello", usage=[])})
    mock.content.search = AsyncMock()
    return mock

@pytest.fixture
def bot_params():
    params = MagicMock()
    params.top_k = 5
    params.min_k = 1
    params.top_n = 3
    params.min_n = 1
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
    retriever._criadex.content.search.side_effect = [
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
    ]
    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response, ContextRetrieverResponse)
    assert response.context is None
    assert len(response.nodes) == 0
    retriever._criadex.agents.cohere.rerank.assert_not_called()

@pytest.mark.asyncio
async def test_retrieve_with_text_context(retriever, bot_mock):
    nodes = [create_text_node("text 1")]
    retriever._criadex.content.search.side_effect = [
        {"response": GroupSearchResponse(nodes=nodes, search_units=1, metadata={}, assets=[]).model_dump()},
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
    ]

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
    nodes = [question_node, create_text_node("other text")]
    retriever._criadex.content.search.side_effect = [
        {"response": GroupSearchResponse(nodes=nodes, search_units=1, metadata={}, assets=[]).model_dump()},
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
    ]

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
    nodes = [question_node, create_text_node("other text")]
    retriever._criadex.content.search.side_effect = [
        {"response": GroupSearchResponse(nodes=nodes, search_units=1, metadata={}, assets=[]).model_dump()},
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
    ]

    retriever.hybrid_rerank = AsyncMock(return_value={"ranked_nodes": nodes, "search_units": 1})

    response = await retriever.retrieve(prompt="hello", metadata_filter=None, extra_bots=[])
    assert isinstance(response.context, TextContext)
    assert "question text" in response.context.text
    assert "other text" not in response.context.text

@pytest.mark.asyncio
async def test_search_groups(retriever, bot_mock):
    retriever._criadex.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }
    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=["extra_bot"])
    assert retriever._criadex.content.search.call_count == len(retriever.INDEX_TYPES) * 2
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
    retriever._criadex.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }
    await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=parent_bots)
    assert retriever._criadex.content.search.call_count == len(retriever.INDEX_TYPES) * 3
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
async def test_search_groups_falls_back_when_graph_payload_invalid(retriever):
    retriever._criadex.manage = MagicMock()
    retriever._criadex.manage.graph_search = AsyncMock(return_value={"unexpected": True})
    retriever._criadex.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }

    result = await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    assert retriever._criadex.manage.graph_search.call_count == len(retriever.INDEX_TYPES)
    assert retriever._criadex.content.search.call_count == len(retriever.INDEX_TYPES)
    assert set(result.keys()) == {"child-document-index", "child-question-index"}


@pytest.mark.asyncio
async def test_search_groups_uses_standard_search_when_graph_disabled(criadex_api, bot_mock, bot_params, monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_CHAT_ENABLED", "false")
    local_retriever = ContextRetriever(
        criadex=criadex_api,
        rerank_model_id=1,
        llm_model_id=1,
        bot=bot_mock,
        bot_params=bot_params
    )
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_search = AsyncMock()
    criadex_api.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }

    await local_retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    criadex_api.manage.graph_search.assert_not_called()
    assert criadex_api.content.search.call_count == len(local_retriever.INDEX_TYPES)

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
