
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.bot.chat.chat import Chat
from criabot.bot.chat.context import TextContext
from criabot.bot.schemas import GroupContentResponse
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel

@pytest.fixture
def bot():
    return AsyncMock()

@pytest.fixture
def chat_model():
    return ChatModel(started_at=123, history=[])

@pytest.fixture
def bot_parameters():
    return BotParametersModel(system_message="system message", max_input_tokens=1000, bot_id=1, id=1)

@pytest.fixture
def chat(bot, chat_model, bot_parameters):
    with patch('criabot.bot.chat.chat.ContextRetriever') as MockContextRetriever:
        retriever_instance = MockContextRetriever.return_value
        retriever_instance.retrieve = AsyncMock(
            return_value=MagicMock(
                context=TextContext(text="some context", nodes=[], related_prompts=[]),
                group_responses={},
                token_usage=[],
                search_units=0
            )
        )
        chat_instance = Chat(
            bot=bot,
            llm_model_id=1,
            rerank_model_id=1,
            chat_model=chat_model,
            bot_parameters=bot_parameters,
            chat_id="test_chat"
        )
        chat_instance._retriever = retriever_instance
        return chat_instance

@pytest.mark.asyncio
async def test_send_text_context(chat, bot):
    bot.criadex.agents.azure.chat = AsyncMock(return_value={"agent_response": {"chat_response": {"message": {"content": "assistant reply"}}}})
    reply = await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    assert reply.content.content == "assistant reply"
    chat._retriever.retrieve.assert_called_once()
    bot.criadex.agents.azure.chat.assert_called_once()
