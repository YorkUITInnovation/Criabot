from typing import Optional, List

from CriadexSDK.routers.content.search import Asset
from CriadexSDK.routers.content.upload import ContentUploadConfig
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute
from criabot.bot.bot import Bot
from criabot.bot.schemas import GroupContentResponse
from criabot.schemas import BotNotFoundError

view = APIRouter()


class UploadDocumentResponse(APIResponse):
    document_name: Optional[str] = None
    token_usage: Optional[int] = None


class DocumentConfig(BaseModel):
    nodes: List[dict]
    assets: List[Asset] = Field(default_factory=list)


class DocumentUploadConfig(ContentUploadConfig):
    file_contents: DocumentConfig


@cbv(view)
class UploadDocumentRoute(CriaRoute):
    ResponseModel = UploadDocumentResponse

    @view.post(
        path="/bots/{bot_name}/documents/upload",
        name="Upload Bot Document",
        summary="Upload a document to the bot",
        description="Upload a document to the bot",
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
            file: DocumentUploadConfig
    ) -> ResponseModel:
        # Try to retrieve the bot
        bot: Bot = await request.app.criabot.get(name=bot_name)

        # Add the documents
        result: GroupContentResponse = await bot.add_group_content(
            file=file,
            index_type="DOCUMENT"
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully added to the index. Save the 'document_name' field to be able to update it!",
            document_name=result.document_name,
            token_usage=result.response.token_usage
        )


__all__ = ["view", "UploadDocumentResponse"]
