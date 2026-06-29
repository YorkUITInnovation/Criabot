from fastapi import Security

import app.core.config as config
from app.core.objects import AppMode
from app.core.security.handlers.bots import GetApiKeyBots
from app.core.security.handlers.master import GetApiKeyMaster
from . import create, delete, about, update, children, parents
from ...core.route import CriaRouter

MASTER_DEPS: list = [Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []
NON_MASTER_DEPS: list = [Security(GetApiKeyBots())] if config.APP_MODE == AppMode.PRODUCTION else []

router = CriaRouter(
    tags=["Bot Management"]
)

about.view.dependencies.extend(NON_MASTER_DEPS)
update.view.dependencies.extend(NON_MASTER_DEPS)
children.view.dependencies.extend(NON_MASTER_DEPS)
parents.view.dependencies.extend(NON_MASTER_DEPS)

create.view.dependencies.extend(MASTER_DEPS)
delete.view.dependencies.extend(MASTER_DEPS)

router.include_views(
    create.view,
    update.view,
    delete.view,
    about.view,
    children.view,
    parents.view,
)

__all__ = ["router"]
