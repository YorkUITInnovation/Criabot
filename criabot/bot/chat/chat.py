from typing import List, Optional, Dict, Tuple

from CriadexSDK import CriadexSDK
from CriadexSDK.routers.agents.azure.chat import ChatMessage, AgentChatRoute, ChatAgentConfig, ChatResponse
from CriadexSDK.routers.content.search import CompletionUsage, Filter, TextNodeWithScore, GroupSearchResponse
from pydantic import BaseModel, Field

from criabot.bot.bot import Bot
from criabot.bot.chat.buffer import ChatBuffer, History
from criabot.bot.chat.context import build_context_prompt, ContextRetriever, QuestionContext, TextContext, Context, \
    build_no_context_guess_prompt, build_no_context_llm_prompt, ContextRetrieverResponse
from criabot.cache.api import BotCacheAPI
from criabot.cache.objects.chats import ChatModel
from criabot.database.bots.tables.bot_params import BotParametersModel


class RelatedPrompt(BaseModel):
    label: str
    prompt: str


class ChatReply(BaseModel):
    prompt: str
    token_usage: List[CompletionUsage]
    total_usage: CompletionUsage
    search_units: int
    content: ChatMessage
    history: List[ChatMessage]
    related_prompts: List[RelatedPrompt] = Field(default_factory=list)
    context: Optional[Context]
    group_responses: Dict[str, GroupSearchResponse]


class Chat:
    """
    Lightweight, transient chat instance

    """

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

        self._bot: Bot = bot
        self._criadex: CriadexSDK = bot.criadex
        self._cache_api: BotCacheAPI = bot.cache_api
        self._chat_model: ChatModel = chat_model
        self._chat_id: str = chat_id
        self._bot_parameters: BotParametersModel = bot_parameters
        self._llm_model_id: int = llm_model_id
        self._rerank_model_id: int = rerank_model_id

        # Build the context retriever
        self._retriever: ContextRetriever = ContextRetriever(
            criadex=self._criadex,
            rerank_model_id=self._rerank_model_id,
            llm_model_id=llm_model_id,
            bot=bot,
            bot_params=bot_parameters
        )

        # Now generate the chat buffer
        self._buffer: ChatBuffer = ChatBuffer(
            max_tokens=self._bot_parameters.max_input_tokens,
            history=chat_model.update_system_message(
                system_message=ChatMessage(
                    role="system",
                    content=bot_parameters.system_message,
                    metadata=self.chat_reply_metadata
                )
            ).history
        )

    @property
    def bot(self) -> Bot:
        """
        Get the bot associated with a chat

        :return: The bot

        """

        return self._bot

    @property
    def chat_reply_metadata(self) -> dict:
        return {"bot_name": self._bot.name}

    @property
    def history(self) -> List[ChatMessage]:
        """
        Retrieve the chat history

        :return: The chat history

        """

        return self._buffer.history

    async def send(
            self,
            prompt: str,
            metadata_filter: Optional[Filter],
            extra_bots: List[str]
    ) -> ChatReply:
        """
        Send a message to the bot and receive a reply

        :param prompt: The user's pre-context message
        :param extra_bots: Extra bots to leverage for IR
        :param metadata_filter: Search the node's metadata with a constraint filter
        :return: None

        """

        # Context, Dict(SearchResponse)
        response: ContextRetrieverResponse = await self._retriever.retrieve(
            prompt=prompt,
            metadata_filter=metadata_filter,
            extra_bots=extra_bots
        )

        # Add the user's prompt to the buffer
        self._buffer.add_message(
            message=ChatMessage(
                role="user",
                content=prompt,
                metadata=self.chat_reply_metadata
            )
        )

        # Generate the response history
        if isinstance(response.context, TextContext):
            reply_history, reply_tokens = await self._text_context_reply(response.context)
        elif isinstance(response.context, QuestionContext):
            reply_history, reply_tokens = self._question_context_reply(response.context)
        elif response.context is None:
            reply_history, reply_tokens = await self._no_context_reply()
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

        # Return reply
        return ChatReply(
            prompt=prompt,
            content=reply_history[-1],  # Reply history unaffected by buffer
            history=reply_history,  # Reply history, which INCLUDES the ephemeral for logging
            group_responses=response.group_responses,
            context=response.context,
            related_prompts=response.context.node.node.metadata.get(ContextRetriever.RELATED_PROMPTS_METADATA_KEY) or [],
            token_usage=token_usage,
            search_units=response.search_units,
            total_usage=CompletionUsage(
                completion_tokens=sum(usage.completion_tokens for usage in token_usage),
                prompt_tokens=sum(usage.prompt_tokens for usage in token_usage),
                total_tokens=sum(usage.total_tokens for usage in token_usage)
            )
        )

    async def _query_llm(self, history: History) -> ChatResponse:
        """Send a chat to the LLM and receive a reply."""

        # Synthesize a reply based on our new info
        response: AgentChatRoute.Response = await self._criadex.agents.azure.chat(
            model_id=self._llm_model_id,
            agent_config=ChatAgentConfig(
                history=history,
                **self._bot_parameters.model_dump()
            )
        )

        # Add metadata to response
        chat_response: ChatResponse = response.verify().agent_response.chat_response
        chat_response.message.metadata = {**chat_response.message.metadata, **self.chat_reply_metadata}

        return chat_response

    def _is_direct_question_reply(
            self,
            group_responses: Dict[str, GroupSearchResponse]
    ) -> bool:
        """
        Check if the group response is a question that requires a direct reply.

        """
        question_response: Optional[GroupSearchResponse] = group_responses.get(self.bot.group_name("QUESTION"))

        if not question_response:
            return False

        try:
            top_response: TextNodeWithScore = question_response.nodes[0]
        except IndexError:
            return False

        # Reverse it, if llm_reply=False, direct question is True
        return not top_response.node.metadata.get("llm_reply")

    async def _text_context_reply(self, context: TextContext) -> Tuple[History, CompletionUsage]:

        # Add the ephemeral context
        buffered_history: History = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                content=build_context_prompt(context, best_guess=self._bot_parameters.no_context_llm_guess),
                metadata=self.chat_reply_metadata
            )
        )

        # Synthesize a reply based on our new info
        chat_response: ChatResponse = await self._query_llm(history=buffered_history)

        # Update buffer & history with the assistant response
        self._buffer.add_message(message=chat_response.message)
        buffered_history.append(chat_response.message)

        return buffered_history, chat_response.raw.usage

    async def _no_context_llm_guess(self) -> Tuple[History, CompletionUsage]:

        # Add the ephemeral best guess prompt
        buffered_history: History = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                content=build_no_context_guess_prompt(
                    no_context_message=(
                        self._bot_parameters.no_context_message
                        if self._bot_parameters.no_context_use_message else None
                    )
                ),
                metadata=self.chat_reply_metadata
            )
        )

        # Synthesize a reply based on our new info
        chat_response: ChatResponse = await self._query_llm(history=buffered_history)

        # Prepend no context message
        if self._bot_parameters.no_context_use_message:
            chat_response.message.content = (
                    self._bot_parameters.no_context_message.strip()
                    + "\n\n"
                    + chat_response.message.content
            )

        # Update buffer & history with the assistant response
        self._buffer.add_message(message=chat_response.message)
        buffered_history.append(chat_response.message)

        return buffered_history, chat_response.raw.usage

    async def _no_context_llm_message(self) -> Tuple[History, CompletionUsage]:

        # Add the ephemeral best guess prompt
        buffered_history: History = self._buffer.buffer(
            system_ephemeral=ChatMessage(
                role="system",
                content=build_no_context_llm_prompt(),
                metadata=self.chat_reply_metadata
            )
        )

        # Synthesize a reply based on our new info
        chat_response: ChatResponse = await self._query_llm(history=buffered_history)

        # Update buffer & history with the assistant response
        self._buffer.add_message(message=chat_response.message)
        buffered_history.append(chat_response.message)

        return buffered_history, chat_response.raw.usage

    def _no_context_saved_message(self) -> Tuple[History, None]:

        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                content=self._bot_parameters.no_context_message,
                metadata=self.chat_reply_metadata
            )
        )

        return self._buffer.history, None

    async def _no_context_reply(
            self,
    ) -> Tuple[History, CompletionUsage]:

        # Case 1) The LLM should guess
        if self._bot_parameters.no_context_llm_guess:
            history, usage = await self._no_context_llm_guess()

        # Case 2) LLM should NOT guess and the saved no context message gets used
        elif self._bot_parameters.no_context_message:
            history, usage = self._no_context_saved_message()

        # Case 3) The LLM should say there's no info
        else:
            history, usage = await self._no_context_llm_message()

        return history, usage

    def _question_context_reply(self, context: QuestionContext) -> Tuple[History, None]:

        # Generate the fake "AI" response
        self._buffer.add_message(
            message=ChatMessage(
                role="assistant",
                content=context.node.node.metadata.get(ContextRetriever.ANSWER_METADATA_KEY),
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
