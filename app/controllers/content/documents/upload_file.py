from typing import Optional

from criabot.criadex_client import CriadexAPIError
from fastapi import APIRouter, UploadFile, File, Form
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import (
    NOT_FOUND_CODE,
    SUCCESS_CODE,
    DUPLICATE_CODE,
    CONTENT_WRITE_RATE_LIMIT,
    exception_response,
    catch_exceptions,
    APIResponse,
    bot_management_limiter,
)
from app.core.route import CriaRoute
from criabot.bot.bot import Bot
from criabot.schemas import BotNotFoundError

view = APIRouter()


class UploadDocumentFileResponse(APIResponse):
    document_name: Optional[str] = None


@cbv(view)
class UploadDocumentFileRoute(CriaRoute):
    ResponseModel = UploadDocumentFileResponse

    @staticmethod
    def _is_duplicate(exc: CriadexAPIError) -> bool:
        if exc.status_code != 409:
            return False
        raw = str(getattr(exc, "message", "")) or str(exc)
        return "DUPLICATE" in raw or "already exists" in raw.lower()

    @view.post(
        path="/bots/{bot_name}/documents/upload/file",
        name="Upload Raw Document File",
        summary="Upload a raw file for native Ragflow parsing",
        description="Upload a PDF, DOCX, HTML, or other file directly. "
                    "Criadex forwards it to Ragflow for native parsing — no CriaParse needed.",
    )
    @bot_management_limiter.limit(CONTENT_WRITE_RATE_LIMIT)
    @catch_exceptions(ResponseModel)
    @exception_response(
        BotNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That bot could not be found!",
        ),
    )
    async def execute(
        self,
        request: Request,
        bot_name: str,
        file: UploadFile = File(...),
        filename_override: Optional[str] = Form(default=None),
        strategy: Optional[str] = Form(default=None),
    ) -> ResponseModel:
        bot: Bot = await request.app.criabot.get(name=bot_name)

        file_name = filename_override or file.filename or "upload"
        file_bytes = await file.read()
        content_type = file.content_type or "application/octet-stream"

        group_name = bot.group_name("DOCUMENT")

        try:
            await request.app.criabot._criadex.content.upload_file(
                group_name=group_name,
                filename=file_name,
                file_bytes=file_bytes,
                content_type=content_type,
                strategy=strategy,
            )
            return self.ResponseModel(
                code=SUCCESS_CODE,
                status=200,
                message="File queued for native Ragflow parsing.",
                document_name=file_name,
            )
        except CriadexAPIError as exc:
            if self._is_duplicate(exc):
                return self.ResponseModel(
                    code=DUPLICATE_CODE,
                    status=409,
                    message="A document with this name already exists. Use a different name or delete it first.",
                )
            raise


__all__ = ["view", "UploadDocumentFileResponse"]
