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