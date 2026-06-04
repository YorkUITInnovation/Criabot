from typing import Optional, List

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class GradebookSyncRequest(BaseModel):
    course_activities: List[dict] = []
    confirmed_mapping: Optional[List[dict]] = None


class GradebookSyncResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    proposal: Optional[dict] = None


@cbv(view)
class GradebookSyncRoute(CriaRoute):
    ResponseModel = GradebookSyncResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/sync",
        name="Sync Moodle gradebook context",
        summary="Merge Moodle activities and mapping into gradebook session",
        description="Updates session course activities and proposal category items from Moodle mapping state.",
    )
    @general_limiter.limit("60/minute")
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
        payload: GradebookSyncRequest,
    ) -> GradebookSyncResponse:
        result = await request.app.criabot.gradebook_sync_moodle_context(
            session_id=session_id,
            course_activities=payload.course_activities,
            confirmed_mapping=payload.confirmed_mapping,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook session synced from Moodle context.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            proposal=result.get("proposal"),
        )


__all__ = ["view"]
