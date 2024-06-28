from abc import ABC, abstractmethod
from typing import TypeVar, Type

from fastapi import APIRouter

from app.controllers.schemas import APIResponse

ResponseModel = TypeVar("ResponseModel", bound=APIResponse)


class CriaRouter(APIRouter):

    def include_views(self, *routers: APIRouter):
        for router in routers:
            self.include_router(
                router
            )


class CriaRoute(ABC):

    @property
    @abstractmethod
    def ResponseModel(self) -> Type[ResponseModel]:
        raise NotImplementedError

    @abstractmethod
    async def execute(self) -> ResponseModel:
        raise NotImplementedError
