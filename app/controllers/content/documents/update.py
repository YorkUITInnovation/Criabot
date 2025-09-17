from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.content.documents.upload import UploadDocumentResponse, DocumentUploadConfig
from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions
from app.core.route import CriaRoute

from criabot.bot.schemas import GroupContentResponse
from criabot.schemas import BotNotFoundError

view = APIRouter()


class UpdateDocumentResponse(UploadDocumentResponse):
    pass


@cbv(view)
class UpdateDocumentRoute(CriaRoute):
    ResponseModel = UpdateDocumentResponse

    @view.patch(
        path="/bots/{bot_name}/documents/update",
        name="Update Bot Document",
        summary="Update a document on the bot",
        description="Update a document on the bot",
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
        from criabot.bot.bot import Bot
        bot: Bot = await request.app.criabot.get(name=bot_name)

        # Add the documents
        result: GroupContentResponse = await bot.update_group_content(
            file=file,
            index_type="DOCUMENT"
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully updated the index",
            document_name=result.document_name,
            token_usage=result.response.token_usage
        )


__all__ = ["view", "UpdateDocumentResponse"]
