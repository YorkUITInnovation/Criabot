from CriadexSDK.routers.content import GroupContentUploadRoute
from pydantic import BaseModel


class ChatNotFoundError(RuntimeError):
    """Raised when trying to delete a nonexistent chat"""

    def __init__(self, chat_id: str):
        self._chat_id: str = chat_id

    @property
    def chat_id(self) -> str:
        return self._chat_id


class GroupContentResponse(BaseModel):
    response: GroupContentUploadRoute.Response
    document_name: str
