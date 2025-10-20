from typing import Optional, Any

from CriadexSDK.ragflow_schemas import CompletionUsage
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, ChatSendConfig, exception_response, catch_exceptions, \
    APIResponse
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
        import logging
        logging.info("Executing query endpoint")
        # Try to get the chat
        from criabot.bot.chat.chat import Chat, ChatReply
        chat: Chat = await request.app.criabot.get_bot_chat(
            bot_name=chat_config.bot_name,
            chat_id=chat_id
        )

        # Check the bots exist
        if chat_config.extra_bots and not await request.app.criabot.exists(*chat_config.extra_bots):
            return self.ResponseModel(
                code=NOT_FOUND_CODE,
                status=404,
                message="One or more bots could not be found in the query."
            )

        reply: ChatReply = await chat.send(
            prompt=chat_config.prompt,
            metadata_filter=chat_config.metadata_filter,
            extra_bots=chat_config.extra_bots
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully sent the query",
            reply=reply
        )


__all__ = ["view"]