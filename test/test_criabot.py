
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.criabot import Criabot, BotExistsError
from criabot.schemas import CriadexCredentials, MySQLCredentials, RedisCredentials, BotCreateConfig

@pytest.fixture
def criadex_credentials():
    return CriadexCredentials(api_base="http://localhost", api_key="test_key")

@pytest.fixture
def mysql_credentials():
    return MySQLCredentials(host="localhost", port=3306, username="root", password="cria", database="criabot")

@pytest.fixture
def redis_credentials():
    return RedisCredentials(host="localhost", port=6379, username="testuser", password="testpass")

@pytest.fixture
def criabot_instance(criadex_credentials, mysql_credentials, redis_credentials):
    with (patch('criabot.criabot.RAGFlowSDK') as MockRAGFlowSDK,
        patch('criabot.criabot.BotDatabaseAPI') as MockBotDatabaseAPI,
        patch('criabot.cache.api.BotCacheAPI') as MockBotCacheAPI,
        patch('criabot.criabot.create_async_engine') as MockEngine):
        criabot = Criabot(criadex_credentials, mysql_credentials, redis_credentials)
        criabot._criadex = MockRAGFlowSDK()
        criabot._mysql_api = MockBotDatabaseAPI()
        criabot._redis_api = MockBotCacheAPI()
        yield criabot

@pytest.mark.asyncio
async def test_create_bot(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
    criabot_instance._criadex.auth.create = AsyncMock(return_value=MagicMock(api_key="new_key"))
    criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
    criabot_instance._criadex.group_auth.create = AsyncMock()
    criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)

    config = BotCreateConfig(llm_model_id=1, embedding_model_id=1, rerank_model_id=1)
    new_auth = await criabot_instance.create(name="new_bot", config=config)

    assert new_auth.api_key == "new_key"
    criabot_instance._criadex.auth.create.assert_called_once()
    assert criabot_instance._criadex.manage.create.call_count == 2
    assert criabot_instance._criadex.group_auth.create.call_count == 2
    criabot_instance._mysql_api.bots.insert.assert_called_once()
    criabot_instance._mysql_api.bot_params.insert.assert_called_once()

@pytest.mark.asyncio
async def test_create_bot_that_exists(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=True)
    with pytest.raises(BotExistsError):
        await criabot_instance.create(name="existing_bot", config=MagicMock())
