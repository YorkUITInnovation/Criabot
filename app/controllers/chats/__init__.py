from typing import List

from fastapi import Security, Depends

from app.core import config
from app.core.objects import AppMode
from app.core.security.handlers.any import GetApiKeyAny
from app.core.security.handlers.bots import GetApiKeyBots
from . import end, exists, history, query, send, start
from ...core.route import CriaRouter

CHATS_ANY_DEPS: List[Depends] = [Security(GetApiKeyAny())] if config.APP_MODE == AppMode.PRODUCTION else []
CHATS_BOT_DEPS: List[Depends] = [Security(GetApiKeyBots())] if config.APP_MODE == AppMode.PRODUCTION else []

router = CriaRouter(
    tags=["Bot Chats"]
)

# Any Deps
end.view.dependencies.extend(CHATS_ANY_DEPS)
exists.view.dependencies.extend(CHATS_ANY_DEPS)
history.view.dependencies.extend(CHATS_ANY_DEPS)
start.view.dependencies.extend(CHATS_ANY_DEPS)

# Bot Deps
query.view.dependencies.extend(CHATS_BOT_DEPS)
send.view.dependencies.extend(CHATS_BOT_DEPS)

# Add views
router.include_views(
    start.view,
    query.view,
    send.view,
    end.view,
    history.view,
    exists.view
)

__all__ = ["router"]
