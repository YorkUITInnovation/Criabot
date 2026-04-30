import pytest
from unittest.mock import AsyncMock, MagicMock

from CriadexSDK.ragflow_schemas import GroupSearchResponse
from criabot.bot.bot import Bot
from criabot.bot.chat.context import ContextRetriever


def expected_empty_search_calls(group_count: int) -> int:
    return (4 * group_count) + (3 * group_count)


@pytest.fixture
def bot_cache_api():
    return AsyncMock()


@pytest.fixture
def criadex_api():
    mock = AsyncMock()
    mock.content.search = AsyncMock()
    return mock


@pytest.fixture
def bot(criadex_api, bot_cache_api):
    return Bot(name="test_bot", criadex=criadex_api, bot_cache=bot_cache_api)


@pytest.fixture
def retriever_bot_params():
    params = MagicMock()
    params.top_k = 5
    params.min_k = 1
    params.top_n = 3
    params.min_n = 1
    return params


@pytest.fixture
def retriever_bot():
    mock = MagicMock()
    mock.group_name.side_effect = lambda index_type: f"child-{index_type.lower()}-index"
    return mock


@pytest.fixture
def retriever(criadex_api, retriever_bot, retriever_bot_params):
    return ContextRetriever(
        criadex=criadex_api,
        rerank_model_id=1,
        llm_model_id=1,
        bot=retriever_bot,
        bot_params=retriever_bot_params
    )


@pytest.mark.asyncio
async def test_search_group_graph_uses_manage_graph_search(bot, criadex_api):
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_status = AsyncMock(return_value={"graph": {"status": "READY"}, "job": {"state": "READY"}})
    criadex_api.manage.graph_search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    result = await bot.search_group_graph("DOCUMENT", {"query": "hello"}, max_hops=2, max_expansion_terms=4)

    criadex_api.manage.graph_search.assert_called_once()
    assert result["group_name"] == "test_bot-document-index"


@pytest.mark.asyncio
async def test_search_group_graph_falls_back_to_standard_search(bot, criadex_api):
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_status = AsyncMock(return_value={"graph": {"status": "READY"}, "job": {"state": "READY"}})
    criadex_api.manage.graph_search = AsyncMock(side_effect=RuntimeError("graph unavailable"))
    criadex_api.content.search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    await bot.search_group_graph("DOCUMENT", {"query": "fallback"})

    criadex_api.content.search.assert_called_once()


@pytest.mark.asyncio
async def test_search_group_graph_triggers_build_when_stale(bot, criadex_api):
    Bot._GRAPH_BUILD_TRIGGERED_AT.clear()
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_status = AsyncMock(return_value={"graph": {"status": "STALE"}, "job": {"state": "READY"}})
    criadex_api.manage.build_graph = AsyncMock(return_value={"job_id": "job-1"})
    criadex_api.manage.graph_search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    await bot.search_group_graph("DOCUMENT", {"query": "rebuild"})

    criadex_api.manage.build_graph.assert_called_once_with(group_name="test_bot-document-index")
    call_kwargs = criadex_api.manage.graph_search.call_args.kwargs
    assert call_kwargs["search_config"]["auto_build"] is True


@pytest.mark.asyncio
async def test_search_group_graph_debounces_rebuild_trigger(bot, criadex_api, monkeypatch):
    Bot._GRAPH_BUILD_TRIGGERED_AT.clear()
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_status = AsyncMock(return_value={"graph": {"status": "STALE"}, "job": {"state": "READY"}})
    criadex_api.manage.build_graph = AsyncMock(return_value={"job_id": "job-1"})
    criadex_api.manage.graph_search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    monkeypatch.setattr("criabot.bot.bot.time.time", lambda: 1000.0)
    await bot.search_group_graph("DOCUMENT", {"query": "first"})
    await bot.search_group_graph("DOCUMENT", {"query": "second"})

    assert criadex_api.manage.build_graph.call_count == 1


@pytest.mark.asyncio
async def test_search_groups_falls_back_when_graph_payload_invalid(retriever):
    retriever._criadex.manage = MagicMock()
    retriever._criadex.manage.graph_search = AsyncMock(return_value={"unexpected": True})
    retriever._criadex.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }

    result = await retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    assert retriever._criadex.manage.graph_search.call_count == len(retriever.INDEX_TYPES)
    assert retriever._criadex.content.search.call_count == expected_empty_search_calls(1)
    assert set(result.keys()) == {"child-document-index", "child-question-index"}


@pytest.mark.asyncio
async def test_search_groups_uses_standard_search_when_graph_disabled(criadex_api, retriever_bot, retriever_bot_params, monkeypatch):
    monkeypatch.setenv("GRAPH_RAG_CHAT_ENABLED", "false")
    local_retriever = ContextRetriever(
        criadex=criadex_api,
        rerank_model_id=1,
        llm_model_id=1,
        bot=retriever_bot,
        bot_params=retriever_bot_params
    )
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_search = AsyncMock()
    criadex_api.content.search.return_value = {
        "response": GroupSearchResponse(nodes=[], search_units=1, metadata={}, assets=[]).model_dump()
    }

    await local_retriever.search_groups(prompt="hello", metadata_filter=None, extra_bots=[])

    criadex_api.manage.graph_search.assert_not_called()
    assert criadex_api.content.search.call_count == expected_empty_search_calls(1)
