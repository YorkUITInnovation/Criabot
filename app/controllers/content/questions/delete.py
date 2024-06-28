from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions
from app.controllers.content.documents.delete import BotContentDeleteResponse
from app.core.route import CriaRoute
from criabot.bot.bot import Bot
from criabot.schemas import BotNotFoundError

view = APIRouter()


@cbv(view)
class DeleteQuestionRoute(CriaRoute):
    ResponseModel = BotContentDeleteResponse

    @view.delete(
        path="/bots/{bot_name}/questions/delete",
        name="Delete Bot Question",
        summary="Delete a question on the bot",
        description="Delete a question on the bot",
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
            document_name: str
    ) -> ResponseModel:
        bot: Bot = await request.app.criabot.get(name=bot_name)

        await bot.delete_group_file(
            index_type="QUESTION",
            document_name=document_name
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully deleted the question from the index."
        )


__all__ = ["view"]
