from fastapi import Security

import app.core.config as app_config
from app.core.objects import AppMode
from app.core.security.handlers.master import GetApiKeyMaster
from app.core.route import CriaRouter
from . import sync, status
from . import config as faq_routes_config

MASTER_DEPS: list = [Security(GetApiKeyMaster())] if app_config.APP_MODE == AppMode.PRODUCTION else []

sync.view.dependencies.extend(MASTER_DEPS)
status.view.dependencies.extend(MASTER_DEPS)
faq_routes_config.view.dependencies.extend(MASTER_DEPS)

router = CriaRouter(tags=["FAQ Management"])
router.include_views(
    sync.view,
    status.view,
    faq_routes_config.view,
)

__all__ = ["router"]
