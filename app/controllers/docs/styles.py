import asyncio
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
    _css_cache: str = None

    async def _read_css(self) -> str:
        """Read CSS file asynchronously"""
        if self._css_cache is None:
            self._css_cache = await asyncio.to_thread(self._read_css_sync)
        return self._css_cache

    def _read_css_sync(self) -> str:
        """Synchronous CSS file reading (runs in thread)"""
        with open(self.CSS_FP, "r", encoding="utf-8") as f:
            return f.read()

    async def get_css(self) -> str:
        """Get CSS for theme"""
        return await self._read_css()

    @view.get(
        "/styles",
        dependencies=[Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []
    )
    @catch_exceptions(
        APIResponse
    )
    async def execute(self) -> ResponseModel:
        css_content = await self.get_css()
        return Response(
            content=css_content,
            headers={"Content-Type": "text/css"}
        )


__all__ = ["view"]
