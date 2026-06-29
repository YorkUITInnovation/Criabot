from typing import List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, BOT_MANAGEMENT_READ_RATE_LIMIT, exception_response, \
    catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute
from criabot.schemas import BotNotFoundError


view = APIRouter()


class BotChildrenResponse(APIResponse):
    children: List[str] = []


@cbv(view)
class ManageChildrenRoute(CriaRoute):
    ResponseModel = BotChildrenResponse

    @view.get(
        path="/bots/{bot_name}/manage/children",
        name="List child bots",
        summary="List child bots for a parent",
        description="Return the direct children for a given parent bot.",
    )
    @bot_management_limiter.limit(BOT_MANAGEMENT_READ_RATE_LIMIT)
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        BotNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That bot could not be found!",
        ),
    )
    async def execute(
        self,
        request: Request,
        bot_name: str,
    ) -> ResponseModel:
        # Ensure the bot exists and then fetch its children by name
        await request.app.criabot.get_id(name=bot_name)

        children = await request.app.criabot.get_children_bot_names(name=bot_name)

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved child bots.",
            children=children,
        )


__all__ = ["view"]

