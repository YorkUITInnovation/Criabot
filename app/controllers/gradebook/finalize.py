from typing import Optional, List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from pydantic import BaseModel

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class ContentMappingItem(BaseModel):
    moodle_cmid: Optional[int] = None
    category: str


class GradebookFinalizeRequest(BaseModel):
    confirmed_mapping: List[ContentMappingItem]
    create_categories: bool = True
    reorganize_resources: bool = False


class GradebookFinalizeResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    summary: Optional[dict] = None


@cbv(view)
class GradebookFinalizeRoute(CriaRoute):
    ResponseModel = GradebookFinalizeResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/finalize",
        name="Finalize gradebook creation",
        summary="Confirm content mapping and trigger Moodle sync",
        description="Confirms the activity-to-category mapping and marks the gradebook as ready for Moodle category creation.",
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
    async def execute(
        self,
        request: Request,
        session_id: str,
        payload: GradebookFinalizeRequest
    ) -> GradebookFinalizeResponse:
        result = await request.app.criabot.gradebook_finalize(
            session_id=session_id,
            confirmed_mapping=[item.model_dump() for item in payload.confirmed_mapping],
            create_categories=payload.create_categories,
            reorganize_resources=payload.reorganize_resources,
        )

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook finalized. Categories and content mapping confirmed.",
            session_id=session_id,
            phase=result["phase"],
            summary=result["summary"],
        )


__all__ = ["view"]