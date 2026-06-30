import uuid
from typing import Type

from criabot.criadex_schemas import ContentUploadConfig
from criabot.criadex_client import CriadexAPIError
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import Field
from starlette.requests import Request

from app.controllers.content.documents.upload import UploadDocumentResponse
from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, DUPLICATE_CODE, APIResponseModel, QuestionConfig, exception_response, \
    CONTENT_WRITE_RATE_LIMIT, catch_exceptions, bot_management_limiter
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

    @staticmethod
    def _is_duplicate_content_error(exc: CriadexAPIError) -> bool:
        if exc.status_code != 409:
            return False
        raw_message = str(getattr(exc, "message", "")) or str(exc)
        return "DUPLICATE" in raw_message or "already exists" in raw_message.lower()

    @view.post(
        path="/bots/{bot_name}/questions/upload",
        name="Upload Bot Question",
        summary="Upload a question to the bot",
        description="Upload a question to the bot",
    )
    @bot_management_limiter.limit(CONTENT_WRITE_RATE_LIMIT)
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
        try:
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
        except CriadexAPIError as ex:
            if self._is_duplicate_content_error(ex):
                return ResponseModel(
                    code=DUPLICATE_CODE,
                    status=409,
                    message="A question with this name already exists in the index. Please use a different name or update the existing question.",
                    document_name=None,
                    token_usage=None
                )
            raise


__all__ = ["view"]
