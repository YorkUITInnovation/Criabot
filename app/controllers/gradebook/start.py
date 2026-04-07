from typing import List, Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, catch_exceptions
from app.core.route import CriaRoute

view = APIRouter()


class StartGradebookConfig(BaseModel):
    course_id: str
    professor_id: str
    bot_name: str
    moodle_resources: List[dict] = Field(default_factory=list)
    course_activities: List[dict] = Field(default_factory=list)


class StartGradebookResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    initial_message: Optional[str] = None


@cbv(view)
class StartGradebookRoute(CriaRoute):
    ResponseModel = StartGradebookResponse

    @view.post(
        path="/gradebook/sessions/start",
        name="Start gradebook session",
        summary="Start gradebook workflow session",
        description="Starts an AI gradebook session based on Moodle resources and activities.",
    )
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, config: StartGradebookConfig) -> StartGradebookResponse:
        result = request.app.criabot.start_gradebook_session(
            course_id=config.course_id,
            professor_id=config.professor_id,
            bot_name=config.bot_name,
            moodle_resources=config.moodle_resources,
            course_activities=config.course_activities,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook session started.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            initial_message=result.get("initial_message"),
        )


__all__ = ["view"]
