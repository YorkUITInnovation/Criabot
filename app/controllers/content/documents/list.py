from typing import Optional, List

from CriadexSDK.ragflow_schemas import ContentListResponse
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, exception_response, catch_exceptions, APIResponse
from app.core.route import CriaRoute

from criabot.schemas import BotNotFoundError

view = APIRouter()


class BotContentListResponse(APIResponse):
    document_names: Optional[List[str]] = None


@cbv(view)
class ListDocumentsRoute(CriaRoute):
    ResponseModel = BotContentListResponse

    @view.get(
        path="/bots/{bot_name}/documents/list",
        name="List Bot Documents",
        summary="List documents stored in the bot",
        description="List documents stored in the bot"
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
        from criabot.bot.bot import Bot
        bot: Bot = await request.app.criabot.get(name=bot_name)
        content: ContentListResponse = await bot.list_group_files(index_type="DOCUMENT")

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved all documents names.",
            document_names=content.get("files")
        )


__all__ = ["view", "BotContentListResponse"]
