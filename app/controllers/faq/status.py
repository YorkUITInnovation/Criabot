from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, catch_exceptions, APIResponse
from app.core.route import CriaRoute

view = APIRouter()


class FAQSyncConfigResponse(BaseModel):
    source_url: str
    group_name: str
    max_pages: int
    timeout_seconds: float


class FAQStatusResponse(APIResponse):
    state: str = "NOT_RUN"
    last_run_at: Optional[int] = None
    last_success_at: Optional[int] = None
    pages_crawled: int = 0
    indexed_files: int = 0
    error: Optional[str] = None
    config: Optional[FAQSyncConfigResponse] = None


@cbv(view)
class FAQStatusRoute(CriaRoute):
    ResponseModel = FAQStatusResponse

    @view.get(
        path="/faq/status",
        name="Get FAQ sync status",
        summary="Read latest FAQ sync state",
        description="Returns latest FAQ sync lifecycle state and current runtime config.",
    )
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request) -> FAQStatusResponse:
        status_data = request.app.criabot.get_faq_sync_status()
        config_data = status_data.get("config", {})
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully retrieved FAQ sync status.",
            state=status_data.get("state", "NOT_RUN"),
            last_run_at=status_data.get("last_run_at"),
            last_success_at=status_data.get("last_success_at"),
            pages_crawled=int(status_data.get("pages_crawled", 0) or 0),
            indexed_files=int(status_data.get("indexed_files", 0) or 0),
            error=status_data.get("error"),
            config=FAQSyncConfigResponse(**config_data) if config_data else None,
        )


__all__ = ["view"]
