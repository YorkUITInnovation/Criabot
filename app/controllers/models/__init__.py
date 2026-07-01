from fastapi import Security

import app.core.config as config
from app.core.objects import AppMode
from app.core.security.handlers.master import GetApiKeyMaster
from . import list as list_
from ...core.route import CriaRouter

MASTER_DEPS: list = [Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []

router = CriaRouter(
    tags=["Models"]
)

list_.view.dependencies.extend(MASTER_DEPS)

router.include_views(
    list_.view,
)

__all__ = ["router"]
