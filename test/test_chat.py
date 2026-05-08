import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.bot.chat.chat import Chat
from criabot.bot.chat.context import TextContext, QuestionContext, ContextRetrieverResponse
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel
from CriadexSDK.ragflow_schemas import TextNodeWithScore, TextNode, ChatMessage
import httpx


def make_text_node(text: str, score: float = 0.9, metadata: dict | None = None):
    return TextNodeWithScore(
        node=TextNode(
            text=text,
            metadata=metadata or {"group_name": "test-document-index"},
            text_template="",
            metadata_template="",
            class_name="",
        ),
        score=score,
    )

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
    # The chat buffer now merges grounding instructions into the main system message.
    call_args = bot_mock.criadex.agents.azure.chat.call_args
    history = call_args[1]['agent_config']['history']
    assert "guess" in history[0]["blocks"][0]["text"]
    assert history[1]["blocks"][0]["text"] == "hello"

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
    # The chat buffer now merges grounding instructions into the main system message.
    call_args = bot_mock.criadex.agents.azure.chat.call_args
    history = call_args[1]['agent_config']['history']
    assert "do not know" in history[0]["blocks"][0]["text"]
    assert history[1]["blocks"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_send_no_context_with_indexing_in_progress(chat, bot_mock):
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(
        context=None,
        group_responses={},
        indexing_in_progress=True,
        indexing_groups=["test-document-index"],
    )

    reply = await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])

    assert reply.content.content == Chat.INDEXING_IN_PROGRESS_MESSAGE
    bot_mock.criadex.agents.azure.chat.assert_not_called()

@pytest.mark.asyncio
async def test_send_with_criadex_error(chat):
    chat._retriever.retrieve.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=MagicMock())
    with pytest.raises(httpx.HTTPStatusError):
        await chat.send(prompt="hello", metadata_filter=None, extra_bots=[])

@pytest.mark.asyncio
async def test_send_with_parent_bots_in_extra_bots(chat, bot_mock):
    parent_bots = ["parent1", "parent2"]
    await chat.send(prompt="hello", metadata_filter=None, extra_bots=parent_bots)
    
    call_args = chat._retriever.retrieve.call_args
    assert call_args[1]['extra_bots'] == parent_bots
    chat._retriever.retrieve.assert_called_once()

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


@pytest.mark.asyncio
async def test_send_single_question_does_not_force_summary(chat, bot_mock):
    nodes = [
        make_text_node("Employee Handbook version 5.4 was released on February 15, 2026."),
        make_text_node("The Advanced Robotics Lab is located in the basement of Building 7, Room B12."),
        make_text_node("The IT Department support motto is QuantumGuard2026."),
    ]
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(
        context=TextContext(text="context", nodes=nodes, related_prompts=[]),
        group_responses={},
    )

    reply = await chat.send(
        prompt="When was Employee Handbook version 5.4 released?",
        metadata_filter=None,
        extra_bots=[],
    )

    assert not reply.content.content.startswith("Summary:\n")
    assert reply.content.content == "Employee Handbook version 5.4 was released on February 15, 2026."
    bot_mock.criadex.agents.azure.chat.assert_not_called()


@pytest.mark.asyncio
async def test_send_simple_factoid_question_uses_direct_top_fact_reply(chat, bot_mock):
    nodes = [
        make_text_node("The IT Department support motto is QuantumGuard2026."),
        make_text_node("Employee Handbook version 5.4 was released on February 15, 2026."),
    ]
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(
        context=TextContext(text="context", nodes=nodes, related_prompts=[]),
        group_responses={},
    )

    reply = await chat.send(
        prompt="What is the IT Department support motto?",
        metadata_filter=None,
        extra_bots=[],
    )

    assert reply.content.content == "The IT Department support motto is QuantumGuard2026."
    bot_mock.criadex.agents.azure.chat.assert_not_called()


@pytest.mark.asyncio
async def test_send_explicit_summary_prompt_uses_summary_fast_path(chat, bot_mock):
    nodes = [
        make_text_node("The Advanced Robotics Lab is located in the basement of Building 7, Room B12."),
        make_text_node("Employee Handbook version 5.4 was released on February 15, 2026."),
        make_text_node("The IT Department support motto is QuantumGuard2026."),
    ]
    chat._retriever.retrieve.return_value = ContextRetrieverResponse(
        context=TextContext(text="context", nodes=nodes, related_prompts=[]),
        group_responses={},
    )

    reply = await chat.send(
        prompt="Give me a summary including the IT Department support motto, the HR handbook release date, and the robotics lab location.",
        metadata_filter=None,
        extra_bots=[],
    )

    assert reply.content.content.startswith("Summary:\n")
    bot_mock.criadex.agents.azure.chat.assert_not_called()
