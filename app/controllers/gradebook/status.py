from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response
from app.core.route import CriaRoute

view = APIRouter()


class GradebookStatusResponse(APIResponse):
    session: Optional[dict] = None


@cbv(view)
class GradebookStatusRoute(CriaRoute):
    ResponseModel = GradebookStatusResponse

    @view.get(
        path="/gradebook/sessions/{session_id}/status",
        name="Get gradebook session status",
        summary="Read gradebook workflow status",
        description="Returns current gradebook phase and persisted session payload.",
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
    async def execute(self, request: Request, session_id: str) -> GradebookStatusResponse:
        session = await request.app.criabot.gradebook_status(session_id=session_id)
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved gradebook session status.",
            session=session,
        )


__all__ = ["view"]
