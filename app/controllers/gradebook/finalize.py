from typing import Optional, List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from pydantic import BaseModel

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class ContentMappingItem(BaseModel):
    grade_item_id: Optional[int] = None
    moodle_cmid: Optional[int] = None
    activity_name: Optional[str] = None
    itemtype: Optional[str] = None
    item_source: Optional[str] = None
    category: str
    subcategory: Optional[str] = None


class GradebookFinalizeRequest(BaseModel):
    confirmed_mapping: List[ContentMappingItem]
    create_categories: bool = True
    reorganize_resources: bool = False


class GradebookFinalizeResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    summary: Optional[dict] = None
    proposal: Optional[dict] = None
    content_mapping: Optional[dict] = None


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

        response_message = "Gradebook finalized. Categories and content mapping confirmed."
        if str(result.get("phase") or "").upper() != "COMPLETED":
            validation = ((result.get("content_mapping") or {}).get("validation") or {})
            errors = list(validation.get("errors") or [])
            if errors:
                response_message = f"Finalize blocked by validation: {errors[0]}"
            else:
                response_message = "Finalize blocked by validation. Resolve proposal checks and try again."

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message=response_message,
            session_id=session_id,
            phase=result["phase"],
            summary=result["summary"],
            proposal=result.get("proposal"),
            content_mapping=result.get("content_mapping"),
        )


__all__ = ["view"]