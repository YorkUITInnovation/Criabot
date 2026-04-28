from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class GradebookUploadConfig(BaseModel):
    filename: str
    filetype: Optional[str] = ""
    base64: str


class GradebookUploadResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    reply: Optional[str] = None
    proposal: Optional[dict] = None


@cbv(view)
class GradebookUploadRoute(CriaRoute):
    ResponseModel = GradebookUploadResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/upload",
        name="Upload gradebook syllabus document",
        summary="Upload syllabus document for gradebook analysis",
        description="Accepts base64-encoded file content and uses extracted text to refine gradebook proposal.",
    )
    @general_limiter.limit("20/minute")
    @catch_exceptions(ResponseModel)
    @exception_response(
        KeyError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="Gradebook session not found.",
        )
    )
    async def execute(self, request: Request, session_id: str, config: GradebookUploadConfig) -> GradebookUploadResponse:
        result = await request.app.criabot.gradebook_upload(
            session_id=session_id,
            filename=config.filename,
            filetype=config.filetype or "",
            base64_content=config.base64,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Document uploaded and processed.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            reply=result.get("reply"),
            proposal=result.get("proposal"),
        )


__all__ = ["view"]
