import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from criabot.criadex_schemas import GroupSearchResponse, TextNode, TextNodeWithScore
from criabot.bot.chat.chat import Chat
from criabot.bot.chat.context import ContextRetriever, ContextRetrieverResponse, TextContext
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel
from criabot.faq.crawler import FAQCrawler
from criabot.faq.fallback import FAQFallback
from criabot.faq.indexer import FAQDocument, FAQIndexer


def create_text_node(text, metadata=None, score=0.8):
    if metadata is None:
        metadata = {}
    return TextNodeWithScore(
        node=TextNode(text=text, metadata=metadata, text_template="", metadata_template="", class_name="TextNode"),
        score=score
    )


@pytest.fixture
def criadex_api():
    mock = AsyncMock()
    mock.agents.cohere.rerank = AsyncMock(return_value={"reranked_documents": [], "search_units": 1})
    mock.agents.azure.transform = AsyncMock(return_value={"agent_response": {"new_prompt": "hello", "usage": []}})
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


@pytest.fixture
def faq_chat():
    bot = AsyncMock()
    bot.criadex.agents.azure.chat = AsyncMock(return_value={"agent_response": {"chat_response": {"message": {"content": "assistant reply"}}}})
    bot.criadex.agents.azure.related_prompts = AsyncMock(return_value=None)
    chat_model = ChatModel(started_at=123, history=[])
    bot_parameters = BotParametersModel(
        system_message="system message",
        max_input_tokens=1000,
        bot_id=1,
        id=1,
        no_context_llm_guess=False,
        no_context_message="",
        llm_generate_related_prompts=False,
        max_reply_tokens=1024,
        temperature=0.9,
        top_p=0,
        top_k=10,
        min_k=0.5,
        top_n=3,
        min_n=0.7,
        no_context_use_message=False
    )

    with patch('criabot.bot.chat.chat.ContextRetriever') as mock_retriever:
        retriever_instance = mock_retriever.return_value
        retriever_instance.retrieve = AsyncMock(
            return_value=ContextRetrieverResponse(
                context=TextContext(text="faq context", nodes=[], related_prompts=[]),
                group_responses={},
                faq_fallback_used=True,
                faq_sources=[{"title": "FAQ Source", "url": "https://lthelp.yorku.ca/eclass"}],
            )
        )
        chat = Chat(
            bot=bot,
            llm_model_id=1,
            rerank_model_id=1,
            chat_model=chat_model,
            bot_parameters=bot_parameters,
            chat_id="test_chat"
        )
        chat._retriever = retriever_instance
        return chat


@pytest.mark.asyncio
async def test_faq_crawler_crawls_single_host_pages():
    pages = {
        "https://lthelp.yorku.ca/eclass": '<html><body><a href="/page-1">One</a>Root FAQ</body></html>',
        "https://lthelp.yorku.ca/page-1": '<html><body><a href="/page-2">Two</a>Page one content</body></html>',
        "https://lthelp.yorku.ca/page-2": '<html><body><a href="https://example.com/offsite">Offsite</a>Page two content</body></html>',
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(status_code=404, request=request)
        return httpx.Response(status_code=200, request=request, headers={"content-type": "text/html"}, text=body)

    transport = httpx.MockTransport(handler)
    crawler = FAQCrawler(
        timeout_seconds=5,
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )
    result = await crawler.crawl("https://lthelp.yorku.ca/eclass", max_pages=10)

    urls = {page["url"] for page in result}
    assert "https://lthelp.yorku.ca/eclass" in urls
    assert "https://lthelp.yorku.ca/page-1" in urls
    assert "https://lthelp.yorku.ca/page-2" in urls
    assert all("example.com" not in page["url"] for page in result)


@pytest.mark.asyncio
async def test_faq_fallback_uses_graph_search_and_dedupes_sources():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(
        return_value={
            "response": {
                "nodes": [
                    {
                        "node": {
                            "text": "Gradebook categories can be configured in setup.",
                            "metadata": {"source_url": "https://lthelp.yorku.ca/eclass", "title": "Gradebook setup", "category": "Gradebook"},
                            "text_template": "",
                            "metadata_template": "",
                            "class_name": "TextNode",
                        },
                        "score": 0.91,
                    },
                    {
                        "node": {
                            "text": "Duplicate source should be deduped.",
                            "metadata": {"source_url": "https://lthelp.yorku.ca/eclass", "title": "Gradebook setup", "category": "Gradebook"},
                            "text_template": "",
                            "metadata_template": "",
                            "class_name": "TextNode",
                        },
                        "score": 0.72,
                    },
                ],
                "assets": [],
                "search_units": 1,
                "metadata": {},
            }
        }
    )
    sdk.content.search = AsyncMock()

    fallback = FAQFallback(criadex=sdk)
    result = await fallback.search(prompt="How to organize gradebook categories?")

    assert result["response"].nodes
    assert len(result["sources"]) == 1
    assert result["sources"][0]["url"] == "https://lthelp.yorku.ca/eclass"
    sdk.content.search.assert_not_called()


@pytest.mark.asyncio
async def test_faq_fallback_falls_back_to_content_search():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(side_effect=RuntimeError("graph unavailable"))
    sdk.content.search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1, "metadata": {}}})

    fallback = FAQFallback(criadex=sdk)
    await fallback.search(prompt="fallback test")

    sdk.content.search.assert_called_once()


@pytest.mark.asyncio
async def test_faq_fallback_caches_repeated_queries():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1, "metadata": {}}})

    fallback = FAQFallback(criadex=sdk)
    fallback._cache.clear()

    await fallback.search(prompt="cache me")
    await fallback.search(prompt="cache me")

    sdk.manage.graph_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_faq_fallback_redis_cache_hit_skips_search():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock()

    faq_cache = MagicMock()
    faq_cache.get = AsyncMock(
        return_value={
            "group_name": "faq-group",
            "response": GroupSearchResponse(nodes=[], assets=[], search_units=0, metadata={}),
            "sources": [],
            "graph_metadata": None,
        }
    )
    faq_cache.set = AsyncMock()

    fallback = FAQFallback(criadex=sdk, faq_cache=faq_cache)
    fallback._cache.clear()

    await fallback.search(prompt="redis cached")

    faq_cache.get.assert_awaited_once()
    sdk.manage.graph_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_faq_fallback_caches_missing_group_from_graph_search():
    class GroupNotFoundError(Exception):
        def __init__(self):
            self.status_code = 404
            self.message = '{"code":"GROUP_NOT_FOUND","message":"Group not found"}'
            super().__init__(self.message)

    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(side_effect=GroupNotFoundError())
    sdk.content.search = AsyncMock()

    fallback = FAQFallback(criadex=sdk)
    fallback._cache.clear()

    first = await fallback.search(prompt="missing faq")
    second = await fallback.search(prompt="missing faq")

    assert first["response"].nodes == []
    assert second["response"].nodes == []
    sdk.manage.graph_search.assert_awaited_once()
    sdk.content.search.assert_not_called()


@pytest.mark.asyncio
async def test_faq_indexer_sync_triggers_graph_build():
    sdk = MagicMock()
    sdk.content = MagicMock()
    sdk.manage = MagicMock()
    sdk.content.upload = AsyncMock(return_value={"token_usage": 10})
    sdk.manage.build_graph = AsyncMock(return_value={"job_id": "job-1", "state": "QUEUED"})

    indexer = FAQIndexer(criadex=sdk)
    docs = [FAQDocument(file_name="faq-1", file_contents={"nodes": [{"text": "FAQ answer", "type": "UncategorizedText", "metadata": {}}]}, file_metadata={"source": "faq"})]
    result = await indexer.sync_group(group_name="eclass-faq-bot-document-index", documents=docs)

    assert result["uploaded_files"] == ["faq-1"]
    assert result["token_usage"] == 10
    assert result["graph_build_job"]["job_id"] == "job-1"
    sdk.manage.build_graph.assert_called_once()


@pytest.mark.asyncio
async def test_faq_indexer_sync_can_skip_graph_build():
    sdk = MagicMock()
    sdk.content = MagicMock()
    sdk.manage = MagicMock()
    sdk.content.upload = AsyncMock(return_value={"token_usage": 5})
    sdk.manage.build_graph = AsyncMock()

    indexer = FAQIndexer(criadex=sdk)
    docs = [FAQDocument(file_name="faq-1", file_contents={"nodes": [{"text": "FAQ answer", "type": "UncategorizedText", "metadata": {}}]}, file_metadata={"source": "faq"})]
    result = await indexer.sync_group(group_name="eclass-faq-bot-document-index", documents=docs, trigger_graph_build=False)

    assert result["graph_build_job"] is None
    sdk.manage.build_graph.assert_not_called()


@pytest.mark.asyncio
async def test_faq_indexer_sync_treats_duplicate_upload_as_non_fatal():
    class DuplicateUploadError(Exception):
        def __init__(self):
            self.status_code = 409
            self.message = '{"code":"DUPLICATE","message":"Requested content already exists in the database."}'
            super().__init__(self.message)

    sdk = MagicMock()
    sdk.content = MagicMock()
    sdk.manage = MagicMock()
    sdk.content.upload = AsyncMock(side_effect=DuplicateUploadError())
    sdk.manage.build_graph = AsyncMock(return_value={"job_id": "job-dup", "state": "QUEUED"})

    indexer = FAQIndexer(criadex=sdk)
    docs = [FAQDocument(file_name="faq-dup", file_contents={"nodes": [{"text": "FAQ duplicate", "type": "UncategorizedText", "metadata": {}}]}, file_metadata={"source": "faq"})]
    result = await indexer.sync_group(group_name="eclass-faq-bot-document-index", documents=docs, trigger_graph_build=True)

    assert result["uploaded_files"] == []
    assert result["duplicate_files"] == ["faq-dup"]
    assert result["graph_build_job"]["job_id"] == "job-dup"


@pytest.mark.asyncio
async def test_retrieve_uses_faq_fallback_on_low_confidence(retriever):
    low_nodes = [create_text_node("weak course context", score=0.1)]
    retriever._criadex.content.search.side_effect = [
        {"response": GroupSearchResponse(nodes=low_nodes, search_units=1, metadata={}, assets=[]).model_dump()},
        {"response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()},
    ]
    retriever._criadex.manage = MagicMock()
    retriever._criadex.manage.graph_search = AsyncMock(
        return_value={
            "response": GroupSearchResponse(
                nodes=[create_text_node("Use Gradebook setup to assign category weights.", metadata={"source_url": "https://lthelp.yorku.ca/eclass", "title": "Gradebook Setup", "category": "Gradebook"}, score=0.92)],
                search_units=1,
                metadata={},
                assets=[],
            ).model_dump()
        }
    )

    response = await retriever.retrieve(prompt="how to set gradebook categories?", metadata_filter=None, extra_bots=[])
    assert response.faq_fallback_used is True
    assert response.context is not None
    assert response.faq_sources[0]["url"] == "https://lthelp.yorku.ca/eclass"


@pytest.mark.asyncio
async def test_send_includes_faq_fallback_metadata(faq_chat):
    reply = await faq_chat.send(prompt="faq question", metadata_filter=None, extra_bots=[])
    assert reply.faq_fallback_used is True
    assert reply.faq_sources[0]["url"] == "https://lthelp.yorku.ca/eclass"
