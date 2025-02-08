import enum
from abc import ABC
from typing import List, Optional, Dict, Union, Literal

from CriadexSDK.routers.agents.azure.chat import ChatMessage
from CriadexSDK.routers.agents.azure.related_prompts import RelatedPrompt
from CriadexSDK.routers.content.search import CompletionUsage, GroupSearchResponse, TextNodeWithScore, Asset
from pydantic import BaseModel, Field


class ContextType(str, enum.Enum):
    QUESTION = "QUESTION"
    TEXT = "TEXT"


class BaseContext(BaseModel, ABC):
    context_type: ContextType = NotImplemented
    related_prompts: List[RelatedPrompt] = []


class QuestionContext(BaseContext):
    context_type: ContextType = ContextType.QUESTION
    file_name: str
    group_name: str
    node: TextNodeWithScore


class TextContext(BaseContext):
    context_type: ContextType = ContextType.TEXT
    text: str
    nodes: List[TextNodeWithScore]


Context = Union[QuestionContext, TextContext]


class ChatReplyContent(BaseModel):
    """Contains the content of the chat reply."""

    role: Literal["assistant", "user"]
    content: str  # <-- Patrick expects a string
    assets: list[Asset] = Field(default_factory=list)  # <-- Assets used in content
    additional_kwargs: dict
    metadata: dict

    @classmethod
    def from_message(
            cls,
            message: ChatMessage,
            assets: list[Asset]
    ) -> "ChatReplyContent":
        # Combine blocks into a single string
        content: str = message.content

        return cls(
            role=message.role,
            content=content,
            assets=assets,
            additional_kwargs=message.additional_kwargs,
            metadata=message.metadata,
        )


class ChatReply(BaseModel):
    prompt: str
    token_usage: List[CompletionUsage]
    total_usage: CompletionUsage
    search_units: int
    content: ChatReplyContent
    history: List[ChatMessage]
    related_prompts: List[RelatedPrompt] = Field(default_factory=list)
    context: Optional[Context]
    group_responses: Dict[str, GroupSearchResponse]
    verified_response: bool
