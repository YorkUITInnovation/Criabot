from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, \
    NOT_FOUND_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.database.bots.tables.bot_params import BotParametersBaseConfig
from criabot.schemas import BotNotFoundError

view = APIRouter()


class BotUpdateResponse(APIResponse):
    pass


@cbv(view)
class ManageUpdateRoute(CriaRoute):
    ResponseModel = BotUpdateResponse

    @view.patch(
        path="/bots/{bot_name}/manage/update",
        name="Configure Bot Hyperparameters",
        summary="Configure Bot Hyperparameters",
        description="Configure the hyperparameters for a Cria Bot",
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
            bot_name: str,
            config: BotParametersBaseConfig
    ) -> ResponseModel:
        # Try to create the bot
        await request.app.criabot.update_parameters(
            name=bot_name,
            params=config
        )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully updated the bot info."
        )


__all__ = ["view"]
