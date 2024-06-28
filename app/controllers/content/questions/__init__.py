from app.controllers.content.questions import upload
from app.core.route import CriaRouter
from . import delete, list, upload, update

router = CriaRouter(
    tags=["Bot Content:Questions"],
)

router.include_views(
    upload.view,
    update.view,
    delete.view,
    list.view
)
__all__ = ["router"]
