from typing import List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.schemas import BotNotFoundError


view = APIRouter()


class BotParentsResponse(APIResponse):
    parents: List[str] = []


@cbv(view)
class ManageParentsRoute(CriaRoute):
    ResponseModel = BotParentsResponse

    @view.get(
        path="/bots/{bot_name}/manage/parents",
        name="List parent bots",
        summary="List parent bots for a child",
        description="Return the direct parents for a given child bot.",
    )
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
        # Ensure the bot exists and then fetch its parents by name
        await request.app.criabot.get_id(name=bot_name)

        parents = await request.app.criabot.get_parent_bot_names(name=bot_name)

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved parent bots.",
            parents=parents,
        )


__all__ = ["view"]
