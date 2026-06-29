from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, BOT_MANAGEMENT_WRITE_RATE_LIMIT, exception_response, catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute
from criabot.schemas import BotNotFoundError

view = APIRouter()


class BotDeleteResponse(APIResponse):
    pass


@cbv(view)
class ManageDeleteRoute(CriaRoute):
    ResponseModel = BotDeleteResponse

    @view.delete(
        path="/bots/{bot_name}/manage/delete",
        name="Delete a Cria Bot",
        summary="Delete a Cria Bot",
        description="Delete a Cria Bot.",
    )
    @bot_management_limiter.limit(BOT_MANAGEMENT_WRITE_RATE_LIMIT)
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
        # Input validation
        if not bot_name or not bot_name.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Bot name cannot be empty."
            )
        
        # Try to delete the bot
        await request.app.criabot.delete(
            name=bot_name
        )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully deleted the bot."
        )


__all__ = ["view"]
