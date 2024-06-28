from CriadexSDK.routers.content import GroupContentListRoute
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions
from app.controllers.content.documents.list import BotContentListResponse
from app.core.route import CriaRoute
from criabot.bot.bot import Bot
from criabot.schemas import BotNotFoundError

view = APIRouter()


@cbv(view)
class ListQuestionsRoute(CriaRoute):
    ResponseModel = BotContentListResponse

    @view.get(
        path="/bots/{bot_name}/questions/list",
        name="List Bot Questions",
        summary="List questions stored in the bot",
        description="List questions stored in the bot"
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
        bot: Bot = await request.app.criabot.get(name=bot_name)
        content: GroupContentListRoute.Response = await bot.list_group_files(index_type="QUESTION")

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved all question (question 'document') names.",
            document_names=content.files
        )


__all__ = ["view"]
