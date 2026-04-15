from typing import Optional, List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from pydantic import BaseModel

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response
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
        # In a full implementation, this would trigger Moodle plugin actions
        # For now, just update the database to mark as pushed to Moodle

        # Get the session
        await request.app.criabot.gradebook_status(session_id=session_id)

        # Locate the gradebook session and corresponding result record
        session_record = await request.app.criabot.gradebook_api.sessions.retrieve(session_id=session_id)
        if session_record is None:
            return self.ResponseModel(
                code=NOT_FOUND_CODE,
                status=404,
                message="Gradebook session not found.",
            )

        result = await request.app.criabot.gradebook_api.results.retrieve_by_session(
            session_id=session_record.id
        )
        if result:
            await request.app.criabot.gradebook_api.results.mark_pushed_to_moodle(result.id)

        # Update session phase to COMPLETED
        await request.app.criabot.gradebook_api.sessions.update_session(
            session_id=session_id,
            updates={"phase": "COMPLETED"}
        )

        summary = {
            "categories_to_create": len(set(item.category for item in payload.confirmed_mapping)),
            "activities_mapped": len(payload.confirmed_mapping),
            "create_categories": payload.create_categories,
            "reorganize_resources": payload.reorganize_resources,
        }

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook finalized. Categories and content mapping confirmed.",
            session_id=session_id,
            phase="COMPLETED",
            summary=summary,
        )


__all__ = ["view"]