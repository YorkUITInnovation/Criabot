import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.criabot import Criabot, BotExistsError, BotNotFoundError
from criabot.schemas import CriadexCredentials, MySQLCredentials, RedisCredentials, BotCreateConfig
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bots import BotsModel
from criabot.database.bots.tables.bot_params import BotParametersModel
from datetime import datetime

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
        patch('criabot.cache.api.BotCacheAPI') as MockBotCacheAPI):
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

@pytest.mark.asyncio
async def test_get_bot(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=True)
    bot = await criabot_instance.get(name="existing_bot")
    assert bot.name == "existing_bot"

@pytest.mark.asyncio
async def test_get_bot_not_found(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
    with pytest.raises(BotNotFoundError):
        await criabot_instance.get(name="non_existing_bot")

@pytest.mark.asyncio
async def test_delete_bot(criabot_instance):
    criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=1)
    criabot_instance.get = AsyncMock(return_value=MagicMock())
    criabot_instance._criadex.manage.delete = AsyncMock(return_value=MagicMock(verify=MagicMock()))
    criabot_instance._mysql_api.bot_params.delete = AsyncMock()
    criabot_instance._mysql_api.bots.delete = AsyncMock()

    await criabot_instance.delete(name="existing_bot")

    assert criabot_instance._criadex.manage.delete.call_count == 2
    criabot_instance._mysql_api.bot_params.delete.assert_called_once_with(bot_id=1)
    criabot_instance._mysql_api.bots.delete.assert_called_once_with(name="existing_bot")

@pytest.mark.asyncio
async def test_about_bot(criabot_instance):
    bots_model = BotsModel(id=1, name="existing_bot", created=datetime.now())
    params_model = BotParametersModel(
        id=1,
        bot_id=1,
        max_input_tokens=1000,
        max_reply_tokens=1024,
        temperature=0.9,
        top_p=0,
        top_k=10,
        min_k=0.5,
        top_n=3,
        min_n=0.7,
        llm_generate_related_prompts=False,
        no_context_message="",
        no_context_use_message=False,
        no_context_llm_guess=False,
        system_message=""
    )
    criabot_instance._mysql_api.bots.retrieve = AsyncMock(return_value=bots_model)
    criabot_instance._mysql_api.bot_params.retrieve = AsyncMock(return_value=params_model)

    about = await criabot_instance.about(name="existing_bot")
    assert about.info == bots_model
    assert about.params == params_model

@pytest.mark.asyncio
async def test_get_bot_chat(criabot_instance):
    criabot_instance._redis_api.chats.get = AsyncMock(return_value=ChatModel(started_at=123, history=[]))
    criabot_instance.about = AsyncMock(return_value=MagicMock(params=MagicMock()))
    criabot_instance.get = AsyncMock(return_value=MagicMock(retrieve_group_info=AsyncMock(return_value=MagicMock(llm_model_id=1, rerank_model_id=1))))
    
    with patch('criabot.bot.chat.chat.Chat') as MockChat:
        await criabot_instance.get_bot_chat(bot_name="test_bot", chat_id="test_chat")
        MockChat.assert_called_once()