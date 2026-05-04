from fastapi import Security

import app.core.config as config
from app.core.objects import AppMode
from app.core.security.handlers.master import GetApiKeyMaster
from app.core.route import CriaRouter
from . import accept, chat, proposal, start, status, finalize, upload, reset, delete

MASTER_DEPS: list = [Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else []

start.view.dependencies.extend(MASTER_DEPS)
chat.view.dependencies.extend(MASTER_DEPS)
proposal.view.dependencies.extend(MASTER_DEPS)
accept.view.dependencies.extend(MASTER_DEPS)
status.view.dependencies.extend(MASTER_DEPS)
finalize.view.dependencies.extend(MASTER_DEPS)
upload.view.dependencies.extend(MASTER_DEPS)
reset.view.dependencies.extend(MASTER_DEPS)
delete.view.dependencies.extend(MASTER_DEPS)

router = CriaRouter(tags=["Gradebook"])
router.include_views(
    start.view,
    chat.view,
    proposal.view,
    accept.view,
    status.view,
    finalize.view,
    upload.view,
    reset.view,
    delete.view,
)

__all__ = ["router"]
