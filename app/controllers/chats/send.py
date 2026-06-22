from typing import Optional, Any, List
import json
import time
from datetime import datetime

from CriadexSDK.ragflow_schemas import CompletionUsage
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, ChatSendConfig, exception_response, catch_exceptions, \
    APIResponse, chat_limiter
from app.core.route import CriaRoute

from criabot.bot.schemas import ChatNotFoundError
from criabot.schemas import BotNotFoundError

view = APIRouter()


class BotChatSendResponse(APIResponse):
    reply: Optional[Any] = None  # Accepts ChatReply, avoids circular import


@cbv(view)
class SendChatRoute(CriaRoute):
    ResponseModel = BotChatSendResponse

    @view.post(
        path="/bots/chats/{chat_id}/send",
        name="Send a Chat",
        summary="Send a chat to a bot",
        description="Send a chat to a bot",
        # dependencies=CHATS_BOT_DEPS
    )
    @chat_limiter.limit("60/minute")
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        ChatNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That chat does not exist or is expired!"
        )
    )
    @exception_response(
        BotNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That bot could not be found!"
        )
    )

    async def execute(
        self,
        request: Request,
        chat_id: str,
        chat_config: ChatSendConfig
    ) -> ResponseModel:
        # Input validation
        if not chat_id or not chat_id.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Chat ID cannot be empty."
            )
        
        if not chat_config.bot_name or not chat_config.bot_name.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Bot name cannot be empty."
            )
        
        if not chat_config.prompt or not chat_config.prompt.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Prompt cannot be empty."
            )
        
        # Try to get the chat
        from criabot.bot.chat.chat import Chat, ChatReply

        chat: Chat = await request.app.criabot.get_bot_chat(
            bot_name=chat_config.bot_name,
            chat_id=chat_id,
        )

        # Resolve inherited parent bots for this bot and merge with explicitly requested extra_bots.
        parent_bot_names: List[str] = await request.app.criabot.get_parent_bot_names(
            name=chat_config.bot_name
        )
        requested_extra_bots: List[str] = chat_config.extra_bots or []
        effective_extra_bots: List[str] = list(
            dict.fromkeys(parent_bot_names + requested_extra_bots)
        )

        # Check explicitly requested extra bots exist (parents are guaranteed by relationships)
        if requested_extra_bots and not await request.app.criabot.exists(*requested_extra_bots):
            return self.ResponseModel(
                code=NOT_FOUND_CODE,
                status=404,
                message="One or more bots could not be found in the query.",
            )

        reply: ChatReply = await chat.send(
            prompt=chat_config.prompt,
            metadata_filter=chat_config.metadata_filter,
            extra_bots=effective_extra_bots,
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully sent the chat",
            reply=reply
        )


@cbv(view)
class StreamChatRoute(CriaRoute):
    """Streaming chat endpoint with reasoning and citations"""

    ResponseModel = BotChatSendResponse

    @view.post(
        path="/bots/chats/{chat_id}/stream",
        name="Stream a Chat",
        summary="Stream a chat response with reasoning steps",
        description="Send a chat to a bot and receive a streaming response with reasoning steps and citations",
    )
    @chat_limiter.limit("60/minute")
    async def execute(
        self,
        request: Request,
        chat_id: str,
        chat_config: ChatSendConfig
    ):
        # Input validation
        if not chat_id or not chat_id.strip():
            return self._stream_error_response("INVALID_INPUT", "Chat ID cannot be empty.")

        if not chat_config.bot_name or not chat_config.bot_name.strip():
            return self._stream_error_response("INVALID_INPUT", "Bot name cannot be empty.")

        if not chat_config.prompt or not chat_config.prompt.strip():
            return self._stream_error_response("INVALID_INPUT", "Prompt cannot be empty.")

        try:
            # Get the chat
            from criabot.bot.chat.chat import Chat, ChatReply

            chat: Chat = await request.app.criabot.get_bot_chat(
                bot_name=chat_config.bot_name,
                chat_id=chat_id,
            )

            # Resolve inherited parent bots
            parent_bot_names: List[str] = await request.app.criabot.get_parent_bot_names(
                name=chat_config.bot_name
            )
            requested_extra_bots: List[str] = chat_config.extra_bots or []
            effective_extra_bots: List[str] = list(
                dict.fromkeys(parent_bot_names + requested_extra_bots)
            )

            # Check explicitly requested extra bots exist
            if requested_extra_bots and not await request.app.criabot.exists(*requested_extra_bots):
                return self._stream_error_response(
                    NOT_FOUND_CODE,
                    "One or more bots could not be found in the query."
                )

            # Generate streaming response
            async def event_generator():
                import asyncio
                
                start_time = time.time()
                status_events = []

                # Create on_step callback to accumulate status events
                async def on_step(engine: str, state: str, message: str):
                    event = {
                        "type": "status",
                        "timestamp": int(time.time() * 1000),
                        "engine": engine,
                        "state": state,
                        "message": message
                    }
                    status_events.append(event)

                # Get the context with streaming status updates
                response = await chat._retriever.retrieve(
                    prompt=chat_config.prompt,
                    metadata_filter=chat_config.metadata_filter,
                    extra_bots=effective_extra_bots,
                    on_step=on_step
                )

                # Yield all status events
                for event in status_events:
                    yield f"data: {json.dumps(event)}\n\n"

                # Add user message to buffer
                from CriadexSDK.ragflow_schemas import ChatMessage
                chat._buffer.add_message(
                    message=ChatMessage(
                        role="user",
                        blocks=[{"type": "text", "text": chat_config.prompt}],
                        additional_kwargs={},
                        metadata=chat.chat_reply_metadata
                    )
                )

                # Emit synthesis start
                synthesis_event = {
                    "type": "status",
                    "timestamp": int(time.time() * 1000),
                    "engine": "synthesis",
                    "state": "start",
                    "message": "🧠 Synthesizing response..."
                }
                yield f"data: {json.dumps(synthesis_event)}\n\n"

                # Generate the reply based on context type
                from criabot.bot.chat.schemas import ChatReply
                from criabot.bot.chat.context import TextContext, QuestionContext

                if isinstance(response.context, TextContext):
                    if chat._should_use_direct_text_reply(response.context, chat_config.prompt):
                        reply_history, reply_tokens = chat._direct_text_context_reply(response.context)
                    elif chat._should_use_direct_text_summary_reply(response.context, chat_config.prompt):
                        reply_history, reply_tokens = chat._direct_text_summary_reply(response.context)
                    else:
                        reply_history, reply_tokens, message_text = await chat._text_context_reply(
                            context=response.context,
                            prompt=chat_config.prompt
                        )
                elif isinstance(response.context, QuestionContext):
                    reply_history, reply_tokens = chat._question_context_reply(response.context)
                elif response.context is None:
                    reply_history, reply_tokens = await chat._no_context_reply(
                        indexing_in_progress=response.indexing_in_progress
                    )
                else:
                    raise ValueError("Unexpected context return case!")

                synthesis_done_event = {
                    "type": "status",
                    "timestamp": int(time.time() * 1000),
                    "engine": "synthesis",
                    "state": "done",
                    "message": "✓ Response synthesized"
                }
                yield f"data: {json.dumps(synthesis_done_event)}\n\n"

                # Emit response content as chunks
                response_message = reply_history[-1]
                response_text = response_message.blocks[0].text if response_message.blocks else ""

                # Split response into chunks (by sentences/paragraphs for better streaming)
                chunks = self._split_response_into_chunks(response_text)
                for chunk in chunks:
                    chunk_event = {
                        "type": "chunk",
                        "timestamp": int(time.time() * 1000),
                        "content": chunk
                    }
                    yield f"data: {json.dumps(chunk_event)}\n\n"

                # Build citations from sources
                citations = self._build_citations(response)

                if citations:
                    citations_event = {
                        "type": "citations",
                        "timestamp": int(time.time() * 1000),
                        "sources": citations
                    }
                    yield f"data: {json.dumps(citations_event)}\n\n"

                # Update cache
                chat._chat_model.history = chat._buffer.history
                await chat._cache_api.chats.set(
                    chat_id=chat_id,
                    chat_model=chat._chat_model
                )

                # Emit done event
                elapsed_ms = int((time.time() - start_time) * 1000)
                done_event = {
                    "type": "done",
                    "timestamp": int(time.time() * 1000),
                    "elapsed_ms": elapsed_ms
                }
                yield f"data: {json.dumps(done_event)}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
            )

        except ChatNotFoundError:
            return self._stream_error_response(NOT_FOUND_CODE, "That chat does not exist or is expired!")
        except BotNotFoundError:
            return self._stream_error_response(NOT_FOUND_CODE, "That bot could not be found!")
        except Exception as e:
            import traceback
            return self._stream_error_response(
                "INTERNAL_ERROR",
                f"An error occurred: {str(e)}"
            )

    def _stream_error_response(self, code: str, message: str):
        """Helper to return error as JSON instead of streaming"""
        from fastapi.responses import JSONResponse
        status_code = 400 if code == "INVALID_INPUT" else 404 if code == NOT_FOUND_CODE else 500
        return JSONResponse(
            status_code=status_code,
            content={"code": code, "status": status_code, "message": message}
        )

    def _split_response_into_chunks(self, text: str, chunk_size: int = 100) -> List[str]:
        """Split response text into manageable chunks for streaming"""
        if not text:
            return []

        # Try to split by sentence boundaries first
        sentences = [s.strip() for s in text.split('.') if s.strip()]
        chunks = []
        current_chunk = ""

        for sentence in sentences:
            if len(current_chunk) + len(sentence) < chunk_size:
                current_chunk += sentence + ". "
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + ". "

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text]

    def _build_citations(self, response) -> List[dict]:
        """Build citation sources from retrieved assets"""
        citations = []
        seen_sources = set()

        # Extract citations from assets
        if response.assets:
            for asset in response.assets:
                source_id = f"asset_{asset.id or asset.file_id}"
                if source_id not in seen_sources:
                    seen_sources.add(source_id)
                    citations.append({
                        "id": source_id,
                        "type": "file",
                        "icon": "📄",
                        "label": asset.file_name or "Document",
                        "display": f"📄 {asset.file_name or 'Document'}",
                        "metadata": {
                            "url": getattr(asset, "file_path", ""),
                            "engine": "elasticsearch",
                            "confidence": float(getattr(asset, "confidence", 0.85))
                        }
                    })

        # Extract web search citations if available
        for group_name, group_response in (response.group_responses or {}).items():
            if group_name == "web_search" and hasattr(group_response, "nodes"):
                for idx, node in enumerate(group_response.nodes):
                    if hasattr(node, "url") and node.url:
                        source_id = f"web_{idx}"
                        if source_id not in seen_sources:
                            seen_sources.add(source_id)
                            citations.append({
                                "id": source_id,
                                "type": "web",
                                "icon": "🌐",
                                "label": getattr(node, "title", node.url),
                                "display": f"🌐 {getattr(node, 'title', node.url)}",
                                "metadata": {
                                    "url": node.url,
                                    "engine": "searxng",
                                    "snippet": getattr(node, "snippet", "")
                                }
                            })

        return citations


__all__ = ["view"]
