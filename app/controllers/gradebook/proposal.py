from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, catch_exceptions
from app.core.route import CriaRoute

view = APIRouter()


class GradebookProposalResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    proposal: Optional[dict] = None


@cbv(view)
class GradebookProposalRoute(CriaRoute):
    ResponseModel = GradebookProposalResponse

    @view.get(
        path="/gradebook/sessions/{session_id}/proposal",
        name="Get gradebook proposal",
        summary="Read latest gradebook proposal",
        description="Returns the proposal payload for the active gradebook session.",
    )
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, session_id: str) -> GradebookProposalResponse:
        result = request.app.criabot.gradebook_proposal(session_id=session_id)
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved gradebook proposal.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            proposal=result.get("proposal"),
        )


__all__ = ["view"]
