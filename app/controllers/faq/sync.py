from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, catch_exceptions, APIResponse, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class FAQSyncConfig(BaseModel):
    source_url: Optional[str] = None
    group_name: Optional[str] = None
    max_pages: Optional[int] = Field(default=None, ge=1, le=500)
    trigger_graph_build: bool = True


class FAQSyncResponse(APIResponse):
    state: str = "QUEUED"
    source_url: Optional[str] = None
    group_name: Optional[str] = None
    pages_crawled: int = 0
    indexed_files: int = 0
    duplicate_files: int = 0
    graph_build_job: Optional[dict] = None


@cbv(view)
class FAQSyncRoute(CriaRoute):
    ResponseModel = FAQSyncResponse

    @view.post(
        path="/faq/sync",
        name="Sync FAQ from website",
        summary="Crawl FAQ website and index into bot KB",
        description="Crawls configured FAQ website pages, uploads to Criadex, and optionally triggers graph build.",
    )
    @general_limiter.limit("10/minute")
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, config: FAQSyncConfig) -> FAQSyncResponse:
        result = await request.app.criabot.sync_faq_site(
            source_url=config.source_url,
            group_name=config.group_name,
            max_pages=config.max_pages,
            trigger_graph_build=config.trigger_graph_build,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="FAQ website sync completed successfully.",
            state="READY",
            source_url=result.get("source_url"),
            group_name=result.get("group_name"),
            pages_crawled=int(result.get("pages_crawled", 0) or 0),
            indexed_files=len(result.get("uploaded_files", [])),
            duplicate_files=len(result.get("duplicate_files", [])),
            graph_build_job=result.get("graph_build_job"),
        )


__all__ = ["view"]
