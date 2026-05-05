from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import APIResponse, SUCCESS_CODE, NOT_FOUND_CODE, catch_exceptions, exception_response, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class GradebookDeleteResponse(APIResponse):
    pass


@cbv(view)
class GradebookDeleteRoute(CriaRoute):
    ResponseModel = GradebookDeleteResponse

    @view.delete(
        path="/gradebook/sessions/{session_id}",
        name="Delete gradebook session",
        summary="Delete gradebook session data",
        description="Deletes gradebook session state from memory/cache/database and removes related persisted result rows.",
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
    async def execute(self, request: Request, session_id: str) -> GradebookDeleteResponse:
        result = await request.app.criabot.gradebook_delete(session_id=session_id)
        
        if not result.get('success'):
            if not result.get('existed'):
                raise KeyError("gradebook session not found")
            # Session existed but couldn't be deleted (shouldn't happen, but handle gracefully)
            status_code = 400
            message = result.get('message', 'Failed to delete session.')
        else:
            status_code = 200
            message = result.get('message', 'Gradebook session deleted.')
        
        return self.ResponseModel(
            code=SUCCESS_CODE if status_code == 200 else NOT_FOUND_CODE,
            status=status_code,
            message=message,
            session_id=session_id,
            data={
                'success': result.get('success'),
                'existed': result.get('existed'),
                'grade_setup_cleaned': result.get('grade_setup_cleaned'),
                'grade_setup_message': result.get('grade_setup_message', ''),
            }
        )


__all__ = ["view"]
