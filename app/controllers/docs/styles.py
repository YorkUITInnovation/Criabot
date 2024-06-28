from pathlib import Path

from fastapi import APIRouter, Security
from fastapi_restful.cbv import cbv
from starlette.responses import Response

from app.controllers.schemas import catch_exceptions, APIResponse
from app.core import config
from app.core.objects import AppMode
from app.core.route import CriaRoute
from app.core.security.handlers.master import GetApiKeyMaster

view = APIRouter()


@cbv(view)
class DocsRedirectRoute(CriaRoute):
    ResponseModel = Response
    CSS_FP: Path = Path(__file__).parent.joinpath("theme.css")
    CSS: str = open(CSS_FP, "r").read()

    def get_css(self) -> str:
        """Get CSS for theme"""

        if config.APP_MODE == AppMode.PRODUCTION:
            return self.CSS
        return open(self.CSS_FP, "r").read()

    @view.get(
        "/styles",
        dependencies=[Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []
    )
    @catch_exceptions(
        APIResponse
    )
    async def execute(self) -> ResponseModel:
        return Response(
            content=self.get_css(),
            headers={"Content-Type": "text/css"}
        )


__all__ = ["view"]
