import logging
from typing import Literal

from fastapi import Security, APIRouter, Depends, Header
from fastapi.openapi.docs import get_swagger_ui_html
from starlette.responses import RedirectResponse, HTMLResponse, Response

import app.core.config as config
from app.controllers import manage, content, chats
from app.core.objects import AppMode
from app.core.security.handlers.master import GetApiKeyMaster
from . import docs


# noinspection PyPep8Naming
def custom_headers(X_Api_Stacktrace: Literal["true", "false"] = Header(default=False)) -> str:
    return X_Api_Stacktrace


router = APIRouter(dependencies=[Depends(custom_headers)])

router.include_router(manage.router)
router.include_router(chats.router)
router.include_router(docs.router)
router.include_router(content.router)

SWAGGER_ROUTE_DEPS: list = [Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []


@router.get("/", include_in_schema=False, dependencies=SWAGGER_ROUTE_DEPS)
async def swagger_ui_custom() -> HTMLResponse:
    """Custom version of the Swagger UI page"""

    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=config.SWAGGER_TITLE,
        swagger_favicon_url=config.SWAGGER_FAVICON
    )


@router.get("/docs", include_in_schema=False)
async def docs_redirect() -> RedirectResponse:
    """
    Redirect "/docs" to "/" endpoint.

    """

    return RedirectResponse(url="/")


class HealthCheckFilter(logging.Filter):
    HEALTH_ENDPOINT: str = "/health_check"

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find(self.HEALTH_ENDPOINT) == -1


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())


@router.get(HealthCheckFilter.HEALTH_ENDPOINT, include_in_schema=False)
async def health_check() -> Response:
    """
    Check if the server is online (for docker health check)
    :return: Just a simple 200

    """

    return Response(status_code=200, content="Pong!")


__all__ = ["router"]
