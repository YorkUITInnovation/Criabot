from typing import Any, Optional

from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from pydantic import BaseModel
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, BOT_MANAGEMENT_READ_RATE_LIMIT, \
    catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute

view = APIRouter()


class RagflowModel(BaseModel):
    id: int
    provider_type: str
    config: dict[str, Any] = {}


class ModelListResponse(APIResponse):
    models: list[RagflowModel] = []


@cbv(view)
class ListModelsRoute(CriaRoute):
    ResponseModel = ModelListResponse

    @view.get(
        path="/models/list",
        name="List Ragflow Models",
        summary="List models available for bot creation",
        description=(
            "List models configured on Ragflow's web interface for the configured tenant. "
            "This is the only set of models a caller should offer for bot/provider selection — "
            "models added directly in Criadex (Azure/Cohere/manual generic rows) are not "
            "Ragflow-backed and are excluded."
        ),
    )
    @bot_management_limiter.limit(BOT_MANAGEMENT_READ_RATE_LIMIT)
    @catch_exceptions(ResponseModel)
    async def execute(
            self,
            request: Request,
    ) -> ResponseModel:
        models = await request.app.criabot.list_ragflow_models()

        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully listed Ragflow models.",
            models=models,
        )


__all__ = ["view", "ModelListResponse", "RagflowModel"]
