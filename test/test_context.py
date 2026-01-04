
import pytest
from unittest.mock import MagicMock
from criabot.bot.chat.context import build_text_context, clean_text, build_context_prompt, ContextRetriever, TextContext

def test_build_text_context():
    nodes = [
        MagicMock(node=MagicMock(text="text 1")),
        MagicMock(node=MagicMock(text="text 2")),
    ]
    context = build_text_context(nodes)
    assert "[DOCUMENT #1]" in context
    assert "text 1" in context
    assert "[DOCUMENT #2]" in context
    assert "text 2" in context

def test_clean_text():
    text = "  hello    world  "
    cleaned_text = clean_text(text)
    assert cleaned_text == "hello world"

def test_build_context_prompt():
    context = TextContext(text="some context", nodes=[], related_prompts=[])
    prompt = build_context_prompt(context)
    assert "[INSTRUCTIONS]" in prompt
    assert "some context" in prompt

def test_is_question_node():
    node = MagicMock()
    node.node.metadata = {"answer": "some answer", "llm_reply": True}
    assert ContextRetriever.is_question_node(node) is True

    node.node.metadata = {"answer": "some answer"}
    assert ContextRetriever.is_question_node(node) is False

def test_is_llm_reply():
    node = MagicMock()
    node.node.metadata = {"answer": "some answer", "llm_reply": True}
    assert ContextRetriever.is_llm_reply(node) is True

    node.node.metadata = {"answer": "some answer", "llm_reply": False}
    assert ContextRetriever.is_llm_reply(node) is False
