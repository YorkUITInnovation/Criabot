from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class GradebookResetConfig(BaseModel):
    keep_extraction: bool = True


class GradebookResetResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    proposal: Optional[dict] = None
    content_mapping: Optional[dict] = None


@cbv(view)
class GradebookResetRoute(CriaRoute):
    ResponseModel = GradebookResetResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/reset",
        name="Reset gradebook session",
        summary="Reset proposal and mapping state",
        description="Resets the gradebook session to a clean proposal state while keeping source resources/activities.",
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
    async def execute(self, request: Request, session_id: str, config: GradebookResetConfig) -> GradebookResetResponse:
        result = await request.app.criabot.gradebook_reset(
            session_id=session_id,
            keep_extraction=config.keep_extraction,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook session reset.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            proposal=result.get("proposal"),
            content_mapping=result.get("content_mapping"),
        )


__all__ = ["view"]
