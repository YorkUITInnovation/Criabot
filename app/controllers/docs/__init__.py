from fastapi import Security

from app.controllers.docs import redirect, swagger, styles, openapi
from app.core import config
from app.core.objects import AppMode
from app.core.route import CriaRouter
from app.core.security.handlers.master import GetApiKeyMaster

router = CriaRouter(
    dependencies=[Security(GetApiKeyMaster())] if config.APP_MODE == AppMode.PRODUCTION else [],
    include_in_schema=False
)

router.include_views(
    redirect.view,
    swagger.view,
    styles.view,
    openapi.view
)
