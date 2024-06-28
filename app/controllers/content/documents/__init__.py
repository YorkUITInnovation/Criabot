from app.controllers.content.documents import upload
from app.core.route import CriaRouter
from . import delete, list, update, upload

router = CriaRouter(
    tags=["Bot Content:Documents"],
)

router.include_views(
    upload.view,
    update.view,
    delete.view,
    list.view
)

__all__ = ["router"]
