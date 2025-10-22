from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.content.documents.update import UpdateDocumentResponse
from app.controllers.content.questions.upload import QuestionUploadConfig
from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions
from app.core.route import CriaRoute

from criabot.bot.schemas import GroupContentResponse
from criabot.schemas import BotNotFoundError

view = APIRouter()


@cbv(view)
class UpdateQuestionRoute(CriaRoute):
    ResponseModel = UpdateDocumentResponse

    @view.patch(
        path="/bots/{bot_name}/questions/update",
        name="Update Bot Question",
        summary="Update a question on the bot",
        description="Update a question on the bot",
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
        file: QuestionUploadConfig
    ) -> ResponseModel:
        # Try to retrieve the bot
        from criabot.bot.bot import Bot
        bot: Bot = await request.app.criabot.get(name=bot_name)

        response: GroupContentResponse = await bot.update_group_content(
            file=file,
            index_type="QUESTION"
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully updated the questions in the index!",
            document_name=response.get("document_name"),
            token_usage=response.get("token_usage")
        )


__all__ = ["view"]
