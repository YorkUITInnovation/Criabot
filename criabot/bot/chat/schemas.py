from typing import List, Optional, Dict, Literal, Type, Union

from CriadexSDK.routers.agents.azure.chat import ChatMessage
from CriadexSDK.routers.agents.azure.related_prompts import RelatedPrompt
from CriadexSDK.routers.content.search import CompletionUsage, GroupSearchResponse, TextNodeWithScore
from pydantic import BaseModel, Field

ContextType: Type = Literal["QUESTION", "TEXT"]


class QuestionContext(BaseModel):
    context_type: ContextType = "QUESTION"
    file_name: str
    group_name: str
    node: TextNodeWithScore
    related_prompts: List[RelatedPrompt] = []


class TextContext(BaseModel):
    context_type: ContextType = "TEXT"
    text: str
    nodes: List[TextNodeWithScore]
    related_prompts: List[RelatedPrompt] = []


Context: Type = Union[QuestionContext, TextContext]


class ChatReply(BaseModel):
    prompt: str
    token_usage: List[CompletionUsage]
    total_usage: CompletionUsage
    search_units: int
    content: ChatMessage
    history: List[ChatMessage]
    related_prompts: List[RelatedPrompt] = Field(default_factory=list)
    context: Optional[Context]
    group_responses: Dict[str, GroupSearchResponse]
    verified_response: bool
