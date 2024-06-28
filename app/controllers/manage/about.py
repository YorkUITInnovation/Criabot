from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, \
    NOT_FOUND_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.schemas import AboutBot, BotNotFoundError

view = APIRouter()


class BotAboutResponse(APIResponse):
    about: Optional[AboutBot] = None


@cbv(view)
class ManageAboutRoute(CriaRoute):
    ResponseModel = BotAboutResponse

    @view.get(
        path="/bots/{bot_name}/manage/about",
        name="About a Cria Bot",
        summary="About a Cria Bot",
        description="Get information about a Cria Bot.",
    )
    @catch_exceptions(
        ResponseModel
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
            bot_name: str
    ) -> ResponseModel:
        # Try to create the bot
        about_model: AboutBot = await request.app.criabot.about(
            name=bot_name
        )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved the bot info.",
            about=about_model
        )


__all__ = ["view"]
