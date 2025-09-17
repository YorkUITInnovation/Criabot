import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.bot.chat.chat import Chat
from criabot.bot.chat.context import TextContext, QuestionContext, ContextRetrieverResponse
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel
from CriadexSDK.ragflow_schemas import TextNodeWithScore, TextNode, ChatMessage
import httpx

@pytest.fixture
def bot_mock():
    bot = AsyncMock()
    bot.criadex.agents.azure.chat = AsyncMock(return_value={"agent_response": {"chat_response": {"message": {"content": "assistant reply"}}}})
    return bot

@pytest.fixture
def chat_model():
    return ChatModel(started_at=123, history=[])

@pytest.fixture
def bot_parameters():
    return BotParametersModel(
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

@pytest.fixture
def chat(bot_mock, chat_model, bot_parameters):
    with patch('criabot.bot.chat.chat.ContextRetriever') as MockContextRetriever:
        retriever_instance = MockContextRetriever.return_value
        retriever_instance.retrieve = AsyncMock(
            return_value=ContextRetrieverResponse(
                context=TextContext(text="some context", nodes=[], related_prompts=[]),
                group_responses={},
                token_usage=[],
                search_units=0
            )
        )
        chat_instance = Chat(
            bot=bot_mock,
            llm_model_id=1,
            rerank_model_id=1,
            chat_model=chat_model,
            bot_parameters=bot_parameters,
            chat_id="test_chat"
        )
        chat_instance._retriever = retriever_instance
        return chat_instance

@pytest.mark.asyncio
async def test_send_with_text_context(chat, bot_mock):
    reply = await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    assert reply.content.content == "assistant reply"
    chat._retriever.retrieve.assert_called_once()
    bot_mock.criadex.agents.azure.chat.assert_called_once()

@pytest.mark.asyncio
async def test_send_with_question_context(chat):
    node = TextNodeWithScore(node=TextNode(text="", metadata={"answer": "direct answer"}, text_template="", metadata_template="", class_name=""), score=0.9)
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(
        context=QuestionContext.model_validate({"node": node.model_dump(), "file_name": "", "group_name": ""}),
        group_responses={}
    )
    reply = await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    assert reply.content.content == "direct answer"

@pytest.mark.asyncio
async def test_send_no_context_with_llm_guess(chat, bot_mock, bot_parameters):
    bot_parameters.no_context_llm_guess = True
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(context=None, group_responses={})
    
    await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    bot_mock.criadex.agents.azure.chat.assert_called_once()
    # Assert that the prompt sent to the LLM contains the "guess" instructions
    call_args = bot_mock.criadex.agents.azure.chat.call_args
    history = call_args[1]['agent_config']['history']
    assert "guess" in history[1].blocks[0].text # ephemeral system message

@pytest.mark.asyncio
async def test_send_no_context_with_saved_message(chat, bot_mock, bot_parameters):
    bot_parameters.no_context_message = "I don't know."
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(context=None, group_responses={})
    
    reply = await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    assert reply.content.content == "I don't know."
    bot_mock.criadex.agents.azure.chat.assert_not_called()

@pytest.mark.asyncio
async def test_send_no_context_with_llm_message(chat, bot_mock):
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(context=None, group_responses={})
    
    await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])
    bot_mock.criadex.agents.azure.chat.assert_called_once()
    # Assert that the prompt sent to the LLM contains the "do not know" instructions
    call_args = bot_mock.criadex.agents.azure.chat.call_args
    history = call_args[1]['agent_config']['history']
    assert "do not know" in history[1].blocks[0].text # ephemeral system message

@pytest.mark.asyncio
async def test_send_with_criadex_error(chat):
    chat._retriever.retrieve.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=MagicMock())
    with pytest.raises(httpx.HTTPStatusError):
        await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])

@pytest.mark.asyncio
async def test_history_management(bot_mock, chat_model, bot_parameters):
    bot_parameters.max_input_tokens = 30
    chat = Chat(
        bot=bot_mock,
        llm_model_id=1,
        rerank_model_id=1,
        chat_model=chat_model,
        bot_parameters=bot_parameters,
        chat_id="test_chat"
    )
    long_string = "a " * 30
    bot_mock.criadex.agents.azure.chat = AsyncMock(return_value={"agent_response": {"chat_response": {"message": {"content": long_string}}}})
    
    with patch('criabot.bot.chat.chat.ContextRetriever.retrieve') as mock_retrieve:
        mock_retrieve.return_value = ContextRetrieverResponse(context=None, group_responses={})
        await chat.send(prompt=long_string, metadata_filter=None, extra_bots=[])
        await chat.send(prompt=long_string, metadata_filter=None, extra_bots=[])
        await chat.send(prompt=long_string, metadata_filter=None, extra_bots=[])

    assert len(chat.history()) <= 4
