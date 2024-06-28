from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from fastapi_restful.cbv import cbv
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.controllers.schemas import catch_exceptions, APIResponse
from app.core import config
from app.core.route import CriaRoute

view = APIRouter()


@cbv(view)
class OpenAPIRoute(CriaRoute):
    ResponseModel = JSONResponse

    @view.get(
        "/openapi.json",
        response_class=JSONResponse
    )
    @catch_exceptions(APIResponse)
    async def execute(self, request: Request) -> ResponseModel:
        openapi_schema = get_openapi(
            title=config.APP_TITLE,
            version=config.APP_VERSION,
            description=config.SWAGGER_DESCRIPTION,
            routes=request.app.routes,
        )

        return JSONResponse(content=openapi_schema)


__all__ = ["view"]
