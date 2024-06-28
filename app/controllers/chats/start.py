from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.bot.bot import Bot

view = APIRouter()


class BotChatStartResponse(APIResponse):
    chat_id: Optional[str] = None


@cbv(view)
class StartChatRoute(CriaRoute):
    ResponseModel = BotChatStartResponse

    @view.post(
        path="/bots/chats/start",
        name="Start Bot Chat",
        summary="Start a chat with a bot",
        description="Start a chat with a bot",
    )
    @catch_exceptions(
        ResponseModel
    )
    async def execute(
            self,
            request: Request
    ) -> ResponseModel:
        # Try to start a chat
        chat_id: str = await Bot.start_chat(cache_api=request.app.criabot.redis_api)

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully started a chat!",
            chat_id=chat_id
        )


__all__ = ["view"]
