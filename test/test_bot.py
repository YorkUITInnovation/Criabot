import pytest
from unittest.mock import AsyncMock, MagicMock
from criabot.bot.bot import Bot

@pytest.fixture
def bot_cache_api():
    return AsyncMock()

@pytest.fixture
def criadex_api():
    return AsyncMock()

@pytest.fixture
def bot(criadex_api, bot_cache_api):
    return Bot(name="test_bot", criadex=criadex_api, bot_cache=bot_cache_api)

@pytest.mark.asyncio
async def test_start_chat(bot_cache_api):
    chat_id = await Bot.start_chat(bot_cache_api)
    assert isinstance(chat_id, str)
    bot_cache_api.chats.set.assert_called_once()


@pytest.mark.asyncio
async def test_start_chat_uses_sdk_for_ensure(bot_cache_api):
    criadex = MagicMock()
    criadex.agents = MagicMock()
    criadex.agents.azure = MagicMock()
    criadex.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})

    chat_id = await Bot.start_chat(bot_cache_api, criadex=criadex)

    assert isinstance(chat_id, str)
    criadex.agents.azure.ensure_dialog.assert_called_once()

def test_group_name(bot):
    assert bot.group_name("QUESTION") == "test_bot-question-index"
    assert bot.group_name("DOCUMENT") == "test_bot-document-index"

@pytest.mark.asyncio
async def test_search_group(bot, criadex_api):
    # Mock the synchronous search method to return a dictionary
    criadex_api.content.search = AsyncMock(return_value={
        "status": 200,
        "message": "Successfully queried the index group 'test_bot-document-index'.",
        "code": "SUCCESS",
        "response": {
            'nodes': [],
            'assets': [],
            'search_units': 1,
            'metadata': {}
        }
    })
    await bot.search_group("DOCUMENT", {})
    criadex_api.content.search.assert_called_once_with(
        group_name="test_bot-document-index",
        search_config={}
    )

@pytest.mark.asyncio
async def test_set_chat_model(bot):
    chat_model = MagicMock()
    await bot.set_chat_model(chat_id="test_chat", chat_model=chat_model)
    bot.cache_api.chats.set.assert_called_once_with(chat_id="test_chat", chat_model=chat_model)


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
    criadex_api.manage.graph_search.assert_called_once()
    call_kwargs = criadex_api.manage.graph_search.call_args.kwargs
    assert call_kwargs["search_config"]["auto_build"] is True


@pytest.mark.asyncio
async def test_search_group_graph_debounces_rebuild_trigger(bot, criadex_api, monkeypatch):
    Bot._GRAPH_BUILD_TRIGGERED_AT.clear()
    criadex_api.manage = MagicMock()
    criadex_api.manage.graph_status = AsyncMock(return_value={"graph": {"status": "STALE"}, "job": {"state": "READY"}})
    criadex_api.manage.build_graph = AsyncMock(return_value={"job_id": "job-1"})
    criadex_api.manage.graph_search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    # Keep time fixed so second call falls within debounce window.
    monkeypatch.setattr("criabot.bot.bot.time.time", lambda: 1000.0)

    await bot.search_group_graph("DOCUMENT", {"query": "first"})
    await bot.search_group_graph("DOCUMENT", {"query": "second"})

    assert criadex_api.manage.build_graph.call_count == 1