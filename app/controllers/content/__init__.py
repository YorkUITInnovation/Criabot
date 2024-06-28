from fastapi import Security, APIRouter

from app.controllers.content import questions, documents
from app.core import config
from app.core.objects import AppMode
from app.core.security.handlers.bots import GetApiKeyBots

CONTENT_DEPS: list = [Security(GetApiKeyBots())] if config.APP_MODE == AppMode.PRODUCTION else []

router = APIRouter(dependencies=CONTENT_DEPS)

router.include_router(documents.router)
router.include_router(questions.router)
