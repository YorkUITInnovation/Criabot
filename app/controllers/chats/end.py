from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, exception_response, \
    catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.bot.schemas import ChatNotFoundError

view = APIRouter()


class BotChatEndResponse(APIResponse):
    pass


@cbv(view)
class EndChatRoute(CriaRoute):
    ResponseModel = BotChatEndResponse

    @view.delete(
        path="/bots/chats/{chat_id}/end",
        name="End a Chat",
        summary="End a chat with a bot",
        description="End a chat with a bot",
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
        await request.app.criabot.end_bot_chat(chat_id)

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully ended the chat!",
            chat_id=chat_id
        )


__all__ = ["view"]
