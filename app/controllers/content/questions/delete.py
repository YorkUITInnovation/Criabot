from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import NOT_FOUND_CODE, \
    SUCCESS_CODE, CONTENT_WRITE_RATE_LIMIT, exception_response, catch_exceptions, bot_management_limiter
from app.controllers.content.documents.delete import BotContentDeleteResponse
from app.core.route import CriaRoute

from criabot.schemas import BotNotFoundError
from criabot.criadex_client import CriadexAPIError

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
                index_type="QUESTION",
                document_name=document_name
            )
        except CriadexAPIError as ex:
            # Idempotent delete: already-missing questions should not fail the flow.
            if ex.status_code != 404:
                raise

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully deleted the question from the index."
        )


__all__ = ["view"]
