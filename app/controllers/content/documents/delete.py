from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, CONTENT_WRITE_RATE_LIMIT, exception_response, catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute

from criabot.schemas import BotNotFoundError
from criabot.criadex_client import CriadexAPIError

view = APIRouter()


class BotContentDeleteResponse(APIResponse):
    pass


@cbv(view)
class DeleteDocumentRoute(CriaRoute):
    ResponseModel = BotContentDeleteResponse

    @view.delete(
        path="/bots/{bot_name}/documents/delete",
        name="Delete Bot Document",
        summary="Delete a document on the bot",
        description="Delete a document on the bot",
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
        document_name: str
    ) -> ResponseModel:
        from criabot.bot.bot import Bot
        bot: Bot = await request.app.criabot.get(name=bot_name)

        try:
            await bot.delete_group_file(
                index_type="DOCUMENT",
                document_name=document_name
            )
        except CriadexAPIError as ex:
            # Idempotent delete: missing content is a valid terminal state.
            if ex.status_code != 404:
                raise

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully deleted the documents from the index."
        )


__all__ = ["view", "BotContentDeleteResponse"]
