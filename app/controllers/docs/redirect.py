from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.controllers.schemas import catch_exceptions, APIResponse
from app.core.route import CriaRoute

view = APIRouter()


@cbv(view)
class DocsRedirectRoute(CriaRoute):
    ResponseModel = RedirectResponse

    @view.get(
        "/",
    )
    @catch_exceptions(
        APIResponse
    )
    async def execute(
            self,
            request: Request
    ) -> ResponseModel:
        return self.ResponseModel(
            url="/docs?" + request.url.query
        )


__all__ = ["view"]
