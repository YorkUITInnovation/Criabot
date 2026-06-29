from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, CHAT_READ_RATE_LIMIT, catch_exceptions, APIResponse, chat_limiter
from app.core.route import CriaRoute

view = APIRouter()


class BotChatExistsResponse(APIResponse):
    exists: Optional[bool] = None


@cbv(view)
class ExistsChatRoute(CriaRoute):
    ResponseModel = BotChatExistsResponse

    @view.get(
        path="/bots/chats/{chat_id}/exists",
        name="Check If Chat Exists",
        summary="Check if the chat with a given Id exists",
        description="Check if the chat with a given Id exists",
    )
    @chat_limiter.limit(CHAT_READ_RATE_LIMIT)
    @catch_exceptions(
        ResponseModel
    )
    async def execute(
            self,
            request: Request,
            chat_id: str
    ) -> ResponseModel:
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message=f"Checked if the chat '{chat_id}' is active!",
            exists=await request.app.criabot.redis_api.chats.exists(chat_id=chat_id)
        )


__all__ = ["view"]
