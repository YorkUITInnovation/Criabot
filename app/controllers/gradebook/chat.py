from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class GradebookChatConfig(BaseModel):
    prompt: str


class GradebookChatResponse(APIResponse):
    session_id: Optional[str] = None
    phase: Optional[str] = None
    reply: Optional[str] = None
    proposal: Optional[dict] = None
    chat_history: Optional[list] = None


@cbv(view)
class GradebookChatRoute(CriaRoute):
    ResponseModel = GradebookChatResponse

    @view.post(
        path="/gradebook/sessions/{session_id}/chat",
        name="Send gradebook chat prompt",
        summary="Advance gradebook workflow by prompt",
        description="Consumes professor feedback and advances the gradebook workflow state machine.",
    )
    @general_limiter.limit("30/minute")
    @catch_exceptions(ResponseModel)
    @exception_response(
        KeyError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="Gradebook session not found.",
        )
    )
    async def execute(self, request: Request, session_id: str, config: GradebookChatConfig) -> GradebookChatResponse:
        result = await request.app.criabot.gradebook_chat(session_id=session_id, prompt=config.prompt)
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Gradebook session updated.",
            session_id=result.get("session_id"),
            phase=result.get("phase"),
            reply=result.get("reply"),
            proposal=result.get("proposal"),
            chat_history=result.get("chat_history"),
        )


__all__ = ["view"]
