from typing import Optional, List

from CriadexSDK.ragflow_schemas import ChatMessage
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, exception_response, \
    catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.bot.schemas import ChatNotFoundError


view = APIRouter()


class BotChatHistoryResponse(APIResponse):
    history: Optional[List[ChatMessage]] = None


@cbv(view)
class ChatHistoryRoute(CriaRoute):
    ResponseModel = BotChatHistoryResponse

    @view.get(
        path="/bots/chats/{chat_id}/history",
        name="Get Chat History",
        summary="Get the current buffered history of a chat",
        description="Get the current buffered history",
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

    async def execute(
        self,
        request: Request,
        chat_id: str
    ) -> ResponseModel:
        # Try to get the chat
        from criabot.cache.objects.chats import ChatModel
        chat_model: ChatModel = await request.app.criabot.redis_api.chats.get(chat_id=chat_id)

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            messsage="Successfully send the chat",
            history=chat_model.history
        )


__all__ = ["view"]
