from app.core.route import CriaRouter
from . import delete, list, update, upload, upload_file

router = CriaRouter(
    tags=["Bot Content:Documents"],
)

router.include_views(
    upload.view,
    upload_file.view,
    update.view,
    delete.view,
    list.view
)

__all__ = ["router"]
