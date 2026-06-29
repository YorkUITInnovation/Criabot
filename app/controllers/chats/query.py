from typing import Optional, Any, List

from CriadexSDK.ragflow_sdk import CriadexNetworkError
from CriadexSDK.ragflow_schemas import CompletionUsage
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, ChatSendConfig, exception_response, catch_exceptions, \
    APIResponse, CHAT_RATE_LIMIT, chat_limiter
from app.core.route import CriaRoute

from criabot.bot.schemas import ChatNotFoundError
from criabot.schemas import BotNotFoundError

view = APIRouter()


class BotChatQueryResponse(APIResponse):
    reply: Optional[Any] = None  # Accepts ChatReply, avoids circular import


@cbv(view)
class QueryChatRoute(CriaRoute):
    ResponseModel = BotChatQueryResponse

    @view.post(
        path="/bots/chats/{chat_id}/query",
        name="Query a Chat",
        summary="Query a bot",
        description="Query a bot",
    )
    @chat_limiter.limit(CHAT_RATE_LIMIT)
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
            chat_id=chat_id
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
                message="One or more bots could not be found in the query."
            )

        try:
            reply: ChatReply = await chat.send(
                prompt=chat_config.prompt,
                metadata_filter=chat_config.metadata_filter,
                extra_bots=effective_extra_bots
            )
        except CriadexNetworkError:
            # Return a graceful response when backend retrieval is temporarily unreachable.
            # This prevents user-facing 500 errors in Moodle while infra recovers.
            return self.ResponseModel(
                code=SUCCESS_CODE,
                status=200,
                message="Temporary service connectivity issue. Please retry in a moment.",
                reply={
                    "message": "I am temporarily unable to reach the knowledge service. Please try again in a few seconds."
                }
            )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully sent the query",
            reply=reply
        )


__all__ = ["view"]