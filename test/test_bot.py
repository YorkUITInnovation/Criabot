
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
    await bot.search_group("DOCUMENT", {})
    criadex_api.content.search.assert_called_once_with(
        group_name="test_bot-document-index",
        search_config={}
    )
