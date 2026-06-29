from typing import Optional, List

from CriadexSDK.ragflow_schemas import Asset, ContentUploadConfig
from CriadexSDK.ragflow_sdk import CriadexAPIError
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, DUPLICATE_CODE, CONTENT_WRITE_RATE_LIMIT, exception_response, catch_exceptions, APIResponse, \
    bot_management_limiter
from app.core.route import CriaRoute
from criabot.bot.schemas import GroupContentResponse
from criabot.schemas import BotNotFoundError

view = APIRouter()


class UploadDocumentResponse(APIResponse):
    document_name: Optional[str] = None
    token_usage: Optional[int] = None


class DocumentConfig(BaseModel):
    nodes: List[dict]
    assets: List[dict] = Field(default_factory=list)


class DocumentUploadConfig(ContentUploadConfig):
    file_contents: DocumentConfig


@cbv(view)
class UploadDocumentRoute(CriaRoute):
    ResponseModel = UploadDocumentResponse

    @staticmethod
    def _is_duplicate_content_error(exc: CriadexAPIError) -> bool:
        if exc.status_code != 409:
            return False
        raw_message = str(getattr(exc, "message", "")) or str(exc)
        return "DUPLICATE" in raw_message or "already exists" in raw_message.lower()

    @view.post(
        path="/bots/{bot_name}/documents/upload",
        name="Upload Bot Document",
        summary="Upload a document to the bot",
        description="Upload a document to the bot",
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
        file: DocumentUploadConfig
    ) -> ResponseModel:
        # Try to retrieve the bot
        from criabot.bot.bot import Bot
        bot: Bot = await request.app.criabot.get(name=bot_name)

        try:
            # Add the documents
            result: GroupContentResponse = await bot.add_group_content(
                file=file,
                index_type="DOCUMENT"
            )

            return self.ResponseModel(
                code=SUCCESS_CODE,
                status=200,
                message="Successfully added to the index. Save the 'document_name' field to be able to update it!",
                document_name=result.get("document_name"),
                token_usage=result.get("token_usage")
            )
        except CriadexAPIError as ex:
            if self._is_duplicate_content_error(ex):
                return self.ResponseModel(
                    code=DUPLICATE_CODE,
                    status=409,
                    message="A document with this name already exists in the index. Please use a different name or update the existing document.",
                    document_name=None,
                    token_usage=None
                )
            raise


__all__ = ["view", "UploadDocumentResponse"]
