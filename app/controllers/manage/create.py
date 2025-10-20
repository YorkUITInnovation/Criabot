from typing import Optional

from CriadexSDK.ragflow_schemas import AuthCreateResponse
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import DUPLICATE_CODE, SUCCESS_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.schemas import BotCreateConfig, BotExistsError

view = APIRouter()


class BotCreateResponse(APIResponse):
    bot_api_key: Optional[str] = None


@cbv(view)
class ManageCreateRoute(CriaRoute):
    ResponseModel = BotCreateResponse

    @view.post(
        path="/bots/{bot_name}/manage/create",
        name="Create a Cria Bot",
        summary="Create a Cria Bot",
        description="Create a Cria Bot.",
    )
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        BotExistsError,
        ResponseModel(
            code=DUPLICATE_CODE,
            status=409,
            message="That bot already exists!"
        )
    )
    async def execute(
            self,
            request: Request,
            bot_name: str,
            config: BotCreateConfig
    ) -> ResponseModel:
        # Try to create the bot
        auth_response: AuthCreateResponse = await request.app.criabot.create(
            name=bot_name,
            config=config
        )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully created the bot & their indexes.",
            bot_api_key=auth_response['api_key']
        )


__all__ = ["view"]