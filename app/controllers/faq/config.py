from typing import Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, catch_exceptions, APIResponse, general_limiter
from app.core.route import CriaRoute

view = APIRouter()


class FAQConfigPatch(BaseModel):
    source_url: Optional[str] = None
    group_name: Optional[str] = None
    max_pages: Optional[int] = Field(default=None, ge=1, le=500)
    timeout_seconds: Optional[float] = Field(default=None, gt=0, le=120)
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = Field(default=None, ge=60, le=604800)
    stale_after_seconds: Optional[int] = Field(default=None, ge=60, le=2592000)
    failure_alert_threshold: Optional[int] = Field(default=None, ge=1, le=100)


class FAQConfigResponse(APIResponse):
    config: dict


@cbv(view)
class FAQConfigRoute(CriaRoute):
    ResponseModel = FAQConfigResponse

    @view.patch(
        path="/faq/config",
        name="Update FAQ sync config",
        summary="Update FAQ crawl runtime config",
        description="Updates website source URL and crawl constraints used by /faq/sync.",
    )
    @general_limiter.limit("30/minute")
    @catch_exceptions(ResponseModel)
    async def execute(self, request: Request, patch: FAQConfigPatch) -> FAQConfigResponse:
        updated = request.app.criabot.update_faq_sync_config(
            source_url=patch.source_url,
            group_name=patch.group_name,
            max_pages=patch.max_pages,
            timeout_seconds=patch.timeout_seconds,
            enabled=patch.enabled,
            interval_seconds=patch.interval_seconds,
            stale_after_seconds=patch.stale_after_seconds,
            failure_alert_threshold=patch.failure_alert_threshold,
        )
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully updated FAQ sync configuration.",
            config=updated,
        )


__all__ = ["view"]
