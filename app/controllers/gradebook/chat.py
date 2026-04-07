from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, catch_exceptions
from app.core.route import CriaRoute

view = APIRouter()


class GradebookChatConfig(BaseModel):
    prompt: str


class GradebookChatResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    reply: Optional[str] = None
    proposal: Optional[dict] = None


@cbv(view)
class GradebookChatRoute(CriaRoute):
    ResponseModel = GradebookChatResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/chat",
        name="Send gradebook chat prompt",
        summary="Advance gradebook workflow by prompt",
        description="Consumes professor feedback and advances the gradebook workflow state machine.",
    )
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, session_id: str, config: GradebookChatConfig) -> GradebookChatResponse:
        result = request.app.criabot.gradebook_chat(session_id=session_id, prompt=config.prompt)
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook session updated.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            reply=result.get("reply"),
            proposal=result.get("proposal"),
        )


__all__ = ["view"]
