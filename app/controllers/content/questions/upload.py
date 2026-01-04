import uuid
from typing import Type

from CriadexSDK.ragflow_schemas import ContentUploadConfig
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import Field
from starlette.requests import Request

from app.controllers.content.documents.upload import UploadDocumentResponse
from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, APIResponseModel, QuestionConfig, exception_response, \
    catch_exceptions
from app.core.route import CriaRoute
from criabot.bot.schemas import GroupContentResponse
from criabot.schemas import BotNotFoundError

ResponseModel: Type[APIResponseModel] = UploadDocumentResponse

view = APIRouter()


class QuestionUploadConfig(ContentUploadConfig):
    file_contents: QuestionConfig
    file_name: str = Field(default_factory=lambda: f"question-{uuid.uuid4()}")


@cbv(view)
class UploadQuestionRoute(CriaRoute):
    ResponseModel = UploadDocumentResponse

    @view.post(
        path="/bots/{bot_name}/questions/upload",
        name="Upload Bot Question",
        summary="Upload a question to the bot",
        description="Upload a question to the bot",
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

        response: GroupContentResponse = await bot.add_group_content(
            file=file,
            index_type="QUESTION"
        )

        return ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully added to the index. Save the 'document_name' field to be able to update it!",
            document_name=response.get("document_name"),
            token_usage=response.get("token_usage")
        )


__all__ = ["view"]
