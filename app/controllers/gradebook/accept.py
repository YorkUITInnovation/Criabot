from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, catch_exceptions
from app.core.route import CriaRoute

view = APIRouter()


class GradebookAcceptResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    proposal: Optional[dict] = None
    content_mapping: Optional[dict] = None


@cbv(view)
class GradebookAcceptRoute(CriaRoute):
    ResponseModel = GradebookAcceptResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/accept",
        name="Accept gradebook proposal",
        summary="Accept proposal and generate mapping",
        description="Marks the proposal accepted and returns generated activity-to-category mapping.",
    )
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, session_id: str) -> GradebookAcceptResponse:
        result = request.app.criabot.gradebook_accept(session_id=session_id)
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook proposal accepted.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            proposal=result.get("proposal"),
            content_mapping=result.get("content_mapping"),
        )


__all__ = ["view"]
