import logging
import traceback
import re
from typing import List, Optional, Dict, Tuple

from CriadexSDK.ragflow_sdk import RAGFlowSDK
from CriadexSDK.ragflow_schemas import ChatMessage, ChatResponse, CompletionUsage, Filter, TextNodeWithScore, GroupSearchResponse

from criabot.bot.bot import Bot
from criabot.bot.chat.buffer import ChatBuffer, History
from criabot.bot.chat.context import (
    build_context_prompt,
    ContextRetriever,
    QuestionContext,
    TextContext,
    build_no_context_guess_prompt,
    build_no_context_llm_prompt,
    ContextRetrieverResponse
)
from criabot.bot.chat.schemas import ChatReply, ChatReplyContent
from criabot.bot.chat.utils import extract_used_assets, strip_asset_data_from_group_responses
from criabot.cache.api import BotCacheAPI
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel


class Chat:
    """
    Lightweight, transient chat instance
    """

    INDEXING_IN_PROGRESS_MESSAGE = (
        "Your document is still indexing. Please try again in a few moments."
    )

    def __init__(
        self,
        bot: Bot,
        llm_model_id: int,
        rerank_model_id: int,
        chat_model: ChatModel,
        bot_parameters: BotParametersModel,
        chat_id: str
    ):

        if not isinstance(chat_model, ChatModel):
            raise ValueError("Must have a valid chat model broski!")

        self._bot = bot
        self._criadex = bot.criadex
        self._cache_api = bot.cache_api
        self._chat_model = chat_model
        self._chat_id = chat_id
        self._bot_parameters = bot_parameters
        self._llm_model_id = llm_model_id
        self._rerank_model_id = rerank_model_id
        self.chat_reply_metadata = {}
        self._related_prompts_enabled = True

        # Build the context retriever
        self._retriever = ContextRetriever(
            criadex=self._criadex,
            rerank_model_id=self._rerank_model_id,
            llm_model_id=llm_model_id,
            bot=bot,
            bot_params=bot_parameters
        )

        # Now generate the chat buffer
        self._buffer = ChatBuffer(
            max_tokens=self._bot_parameters.max_input_tokens,
            history=chat_model.update_system_message(
                system_message=ChatMessage(
                    role="system",
                    blocks=[{"type": "text", "text": bot_parameters.system_message}],
                    additional_kwargs={},
                    metadata=self.chat_reply_metadata
                )
            ).history
        )

    @property
    def bot(self) -> Bot:
        """Get the bot associated with a chat"""
        return self._bot

    def history(self) -> List[ChatMessage]:
        """Retrieve the chat history"""
        return self._buffer.history

    async def send(
        self,
        prompt: str,
        metadata_filter: Optional[Filter],
        extra_bots: List[str]
    ) -> ChatReply:
        """Send a message to the bot and receive a reply"""

        # Context, Dict(SearchResponse)
        response: ContextRetrieverResponse = await self._retriever.retrieve(
            prompt=prompt,
            metadata_filter=metadata_filter,
            extra_bots=extra_bots
        )
        if response.indexing_in_progress:
            self.chat_reply_metadata["indexing_in_progress"] = True
            self.chat_reply_metadata["indexing_groups"] = response.indexing_groups
        if response.faq_fallback_used:
            self.chat_reply_metadata["faq_fallback_used"] = True
            self.chat_reply_metadata["faq_sources"] = response.faq_sources

        # Add the user's prompt to the buffer
        self._buffer.add_message(
            message=ChatMessage(
                role="user",
                blocks=[{"type": "text", "text": prompt}],
                additional_kwargs={},
                metadata=self.chat_reply_metadata
            )
        )

        # Generate the response history
        if isinstance(response.context, TextContext):
            if self._should_use_direct_text_reply(response.context, prompt):
                reply_history, reply_tokens = self._direct_text_context_reply(response.context)
            elif self._should_use_direct_text_summary_reply(response.context, prompt):
                reply_history, reply_tokens = self._direct_text_summary_reply(response.context)
            else:
                reply_history, reply_tokens, message_text = await self._text_context_reply(
                    context=response.context,
                    prompt=prompt
                )
        elif isinstance(response.context, QuestionContext):
            reply_history, reply_tokens = self._question_context_reply(response.context)
        elif response.context is None:
            reply_history, reply_tokens = await self._no_context_reply(
                indexing_in_progress=response.indexing_in_progress
            )
        else:
            raise ValueError("Unexpected context return case!")

        # Add the token usage
        token_usage: List[CompletionUsage] = ([reply_tokens] if reply_tokens else []) + response.token_usage

        # Update the chat model with our new *actual* history (excludes ephemeral)
        self._chat_model.history = self._buffer.history

        # Update cache with our updated chat model
        await self._cache_api.chats.set(
            chat_id=self._chat_id,
            chat_model=self._chat_model
        )

        response_message: ChatMessage = reply_history[-1]

        related_prompts = response.context.related_prompts if response.context else []
        if self._bot_parameters.llm_generate_related_prompts and self._related_prompts_enabled and not related_prompts:
            try:
                related_prompts_response = await self._criadex.agents.azure.related_prompts(
                    model_id=self._llm_model_id,
                    agent_config={
                        "llm_prompt": prompt,
                        "llm_reply": response_message.blocks[0].text,
                        "max_reply_tokens": 500,
                        "temperature": 0.1
                    }
                )
                if related_prompts_response and related_prompts_response.get('agent_response'):
                    related_prompts = related_prompts_response['agent_response'].get('related_prompts', [])
                    usage_from_related_prompts = related_prompts_response['agent_response'].get('usage', [])
                    if usage_from_related_prompts and isinstance(usage_from_related_prompts[0], dict):
                        token_usage.extend([CompletionUsage(**u) for u in usage_from_related_prompts])
                    else:
                        token_usage.extend(usage_from_related_prompts)
            except Exception as exc:
                # Related prompts are optional. If network/DNS is flaky, avoid repeated retries/log spam for this chat.
                message = str(exc)
                if "Name or service not known" in message or "Network error after" in message:
                    self._related_prompts_enabled = False
                    logging.warning(
                        "Related prompts disabled for chat_id=%s due to transient network/DNS error: %s",
                        self._chat_id,
                        message,
                    )
                else:
                    logging.error("Failed to generate related prompts! " + traceback.format_exc())

        # Return reply
        return ChatReply(
            prompt=prompt,
            content=ChatReplyContent.from_message(
                message=response_message,
                assets=extract_used_assets(assets=response.assets, text=response_message.blocks[0].text)
            ),
            history=[m.model_dump() for m in reply_history],
            group_responses=strip_asset_data_from_group_responses(response.group_responses),
            context=response.context,
            related_prompts=related_prompts,
            token_usage=token_usage,
            search_units=response.search_units,
            verified_response=response.context.context_type == "QUESTION" if response.context else False,
            faq_fallback_used=response.faq_fallback_used,
            faq_sources=response.faq_sources,
            total_usage={
                "completion_tokens": sum(usage.completion_tokens for usage in token_usage),
                "prompt_tokens": sum(usage.prompt_tokens for usage in token_usage),
                "total_tokens": sum(usage.total_tokens for usage in token_usage),
                "usage_label": "All"
            },
        )

    async def _query_llm(self, history):
        """Send a chat to the LLM and receive a reply."""

        # Synthesize a reply based on our new info
        agent_config = {
            "history": [msg.model_dump() for msg in history],
            "chat_id": self._chat_id,
            **self._bot_parameters.model_dump()
        }
        response = await self._criadex.agents.azure.chat(
            model_id=self._llm_model_id,
            agent_config=agent_config
        )
        if isinstance(response, dict):
            if "agent_response" in response:
                chat_response = response["agent_response"]["chat_response"]
            else:
                raise KeyError("'agent_response' key missing in Criadex chat response")
        else:
            chat_response = response.verify().agent_response.chat_response

        chat_response["message"]["metadata"] = {
            **chat_response["message"].get("metadata", {}),
            **self.chat_reply_metadata
        } if isinstance(chat_response, dict) else {
            **chat_response.message.metadata,
            **self.chat_reply_metadata
        }

        return chat_response

    def _is_direct_question_reply(self, group_responses: Dict[str, GroupSearchResponse]) -> bool:
        """Check if the group response is a question that requires a direct reply."""
        question_response: Optional[GroupSearchResponse] = group_responses.get(self.bot.group_name("QUESTION"))

        if not question_response:
            return False

        try:
            top_response: TextNodeWithScore = question_response.nodes[0]
        except IndexError:
            return False

        # Reverse it, if llm_reply=False, direct question is True
        return not top_response.node.metadata.get("llm_reply")

    async def _text_context_reply(self, context, prompt: str):
        # Add the ephemeral context
        buffered_history = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                blocks=[{
                    "type": "text",
                    "text": build_context_prompt(
                        context,
                        prompt=prompt,
                        best_guess=self._bot_parameters.no_context_llm_guess
                    )
                }],
                additional_kwargs={},
                metadata=self.chat_reply_metadata
            )
        )
        # Synthesize a reply based on our new info
        chat_response = await self._query_llm(history=buffered_history)
        if isinstance(chat_response, dict):
            msg = chat_response["message"]
            if isinstance(msg, dict):
                msg = ChatMessage(
                    role=msg.get("role", "assistant"),
                    blocks=msg.get("blocks", [{"type": "text", "text": msg.get("content", "")}]),
                    additional_kwargs=msg.get("additional_kwargs", {}),
                    metadata=msg.get("metadata", {})
                )
            self._buffer.add_message(message=msg)
            buffered_history.append(msg)
            usage_dict = chat_response.get("usage", None)
            reply_tokens = CompletionUsage(**usage_dict) if usage_dict else None
            return buffered_history, reply_tokens, chat_response.get("message", {}).get("content", "")
        else:
            self._buffer.add_message(message=chat_response.message)
            buffered_history.append(chat_response.message)
            return buffered_history, chat_response.raw.usage, chat_response.message.content

    # ↓↓↓ DE-INDENTED FUNCTIONS ↓↓↓
    async def _no_context_llm_guess(self):
        # Add the ephemeral best guess prompt
        buffered_history = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                blocks=[{"type": "text", "text": build_no_context_guess_prompt(
                    no_context_message=(
                        self._bot_parameters.no_context_message
                        if self._bot_parameters.no_context_use_message else None
                    )
                )}],
                additional_kwargs={},
                metadata=self.chat_reply_metadata
            )
        )
        # Synthesize a reply based on our new info
        chat_response = await self._query_llm(history=buffered_history)
        if self._bot_parameters.no_context_use_message:
            if isinstance(chat_response, dict):
                chat_response["message"]["blocks"] = {
                    "text": self._bot_parameters.no_context_message.strip() + "\n\n" + chat_response["message"]["content"]
                }
            else:
                chat_response.message.blocks = type(chat_response.message.blocks)(
                    text=(
                        self._bot_parameters.no_context_message.strip()
                        + "\n\n"
                        + chat_response.message.content
                    )
                )

        if isinstance(chat_response, dict):
            msg = chat_response["message"]
            if isinstance(msg, dict):
                msg = ChatMessage(
                    role=msg.get("role", "assistant"),
                    blocks=[{"type": "text", "text": msg.get("content", "")}],
                    additional_kwargs=msg.get("additional_kwargs", {}),
                    metadata=msg.get("metadata", {})
                )
            self._buffer.add_message(message=msg)
            buffered_history.append(msg)
            usage_dict = chat_response.get("usage", None)
            reply_tokens = CompletionUsage(**usage_dict) if usage_dict else None
            return buffered_history, reply_tokens
        else:
            self._buffer.add_message(message=chat_response.message)
            buffered_history.append(chat_response.message)
            return buffered_history, chat_response.raw.usage

    async def _no_context_llm_message(self):
        # Add the ephemeral best guess prompt
        buffered_history = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                blocks=[{"type": "text", "text": build_no_context_llm_prompt()}],
                additional_kwargs={},
                metadata=self.chat_reply_metadata
            )
        )
        # Synthesize a reply based on our new info
        chat_response = await self._query_llm(history=buffered_history)
        if isinstance(chat_response, dict):
            msg = chat_response["message"]
            if isinstance(msg, dict):
                msg = ChatMessage(
                    role=msg.get("role", "assistant"),
                    blocks=[{"type": "text", "text": msg.get("content", "")}],
                    additional_kwargs=msg.get("additional_kwargs", {}),
                    metadata=msg.get("metadata", {})
                )
            self._buffer.add_message(message=msg)
            buffered_history.append(msg)
            usage_dict = chat_response.get("usage", None)
            reply_tokens = CompletionUsage(**usage_dict) if usage_dict else None
            return buffered_history, reply_tokens
        else:
            self._buffer.add_message(message=chat_response.message)
            buffered_history.append(chat_response.message)
            return buffered_history, chat_response.raw.usage

    def _no_context_saved_message(self):
        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                blocks=[{"type": "text", "text": self._bot_parameters.no_context_message}],
                additional_kwargs={},
                metadata=self.chat_reply_metadata
            )
        )
        return self._buffer.history, None

    def _indexing_in_progress_message(self):
        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                blocks=[{"type": "text", "text": self.INDEXING_IN_PROGRESS_MESSAGE}],
                additional_kwargs={},
                metadata=self.chat_reply_metadata,
            )
        )
        return self._buffer.history, None

    async def _no_context_reply(self, indexing_in_progress: bool = False):
        if indexing_in_progress:
            return self._indexing_in_progress_message()

        if self._bot_parameters.no_context_llm_guess:
            history, usage = await self._no_context_llm_guess()
            
        # Case 2) LLM should NOT guess and the saved no context message gets used
        elif self._bot_parameters.no_context_message:
            history, usage = self._no_context_saved_message()

        # Case 3) The LLM should say there's no info
        else:
            history, usage = await self._no_context_llm_message()

        return history, usage

    # ↑↑↑ END DE-INDENTED FUNCTIONS ↑↑↑

    def _question_context_reply(self, context):

        # Generate the fake "AI" response
        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                blocks=[{"type": "text", "text": context.node.node.metadata.get(ContextRetriever.ANSWER_METADATA_KEY)}],
                additional_kwargs={},
                metadata={
                    "no_llm_reply": {
                        "file_name": context.node.node.metadata.get(ContextRetriever.FILE_NAME_METADATA_KEY),
                        "group_name": context.node.node.metadata.get(ContextRetriever.GROUP_NAME_METADATA_KEY)
                    },
                    **self.chat_reply_metadata
                }
            )
        )
        return self._buffer.history, None

    def _should_use_direct_text_reply(self, context: TextContext, prompt: str) -> bool:
        fact_texts = self._extract_fact_texts(context)
        if not fact_texts:
            return False

        top_fact_text = fact_texts[0]
        if len(top_fact_text) > 300:
            return False

        # If the top fact looks like a pointer to another section rather than 
        # the answer itself, avoid a direct reply so the LLM can try to 
        # synthesize a better response from all retrieved context.
        pointer_pattern = r"\b(please\s+)?refer\s+to\b|\bsee\s+(section|chapter|appendix|page|module)\b"
        if re.search(pointer_pattern, top_fact_text, re.IGNORECASE):
            return False

        if len(context.nodes) == 1:
            return True

        # Simple factoid prompts are more reliable when answered from the top
        # retrieved fact instead of letting the LLM rewrite or replace it.
        return ContextRetriever._extract_focused_question_prompt(prompt) is not None and len(context.nodes) <= 3

    def _direct_text_context_reply(self, context: TextContext):
        node = context.nodes[0]
        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                blocks=[{"type": "text", "text": node.node.text}],
                additional_kwargs={},
                metadata={
                    "direct_context_reply": {
                        "file_name": node.node.metadata.get(ContextRetriever.FILE_NAME_METADATA_KEY),
                        "group_name": node.node.metadata.get(ContextRetriever.GROUP_NAME_METADATA_KEY),
                    },
                    **self.chat_reply_metadata,
                }
            )
        )
        return self._buffer.history, None

    def _should_use_direct_text_summary_reply(self, context: TextContext, prompt: str) -> bool:
        normalized_prompt = (prompt or "").strip()
        summary_requested = bool(ContextRetriever._PROMPT_PREFIX_RE.search(normalized_prompt))
        if not summary_requested:
            return False

        fact_texts = self._extract_fact_texts(context)
        if len(fact_texts) < 2 or len(fact_texts) > 5:
            return False

        total_length = sum(len(text) for text in fact_texts)
        return total_length <= 1200

    def _direct_text_summary_reply(self, context: TextContext):
        fact_texts = self._extract_fact_texts(context)
        summary_text = "Summary:\n" + "\n".join(f"- {text}" for text in fact_texts)

        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                blocks=[{"type": "text", "text": summary_text}],
                additional_kwargs={},
                metadata={
                    "direct_context_summary_reply": {
                        "groups": [
                            node.node.metadata.get(ContextRetriever.GROUP_NAME_METADATA_KEY)
                            for node in context.nodes[:len(fact_texts)]
                        ],
                    },
                    **self.chat_reply_metadata,
                }
            )
        )
        return self._buffer.history, None

    def _extract_fact_texts(self, context: TextContext) -> List[str]:
        fact_texts: list[str] = []
        seen: set[str] = set()

        for node in context.nodes:
            text = (node.node.text or "").strip()
            if not text or len(text) > 300 or text in seen:
                continue
            seen.add(text)
            fact_texts.append(text)

        return fact_texts
