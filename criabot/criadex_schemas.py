"""
Local copies of the Pydantic schemas previously provided by CriadexSDK.ragflow_schemas.

Moved here as part of Phase 3 CriadexSDK removal so Criabot owns its own types
without an external SDK dependency.
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, AnyUrl, FilePath

# --- Auth ---

class AuthCreateConfig(BaseModel):
    master: bool = False

class AuthCreateResponse(BaseModel):
    api_key: str
    master: Optional[bool] = False

class AuthCheckResponse(BaseModel):
    api_key: Optional[str]
    master: Optional[bool]
    authorized: Optional[bool]

class AuthDeleteResponse(BaseModel):
    api_key: Optional[str]

class AuthResetResponse(BaseModel):
    new_key: Optional[str]

# --- Group ---

class GroupCreateConfig(BaseModel):
    type: str
    llm_model_id: int
    embedding_model_id: int
    rerank_model_id: int
    use_knowledge_graph: bool = True

class GroupCreateResponse(BaseModel):
    name: str
    type: str
    llm_model_id: int
    embedding_model_id: int
    rerank_model_id: int
    use_knowledge_graph: Optional[bool] = None
    id: Optional[int]
    created: Optional[str]

class GroupInfo(GroupCreateConfig):
    id: int
    created: str

class GroupAboutResponse(BaseModel):
    info: Optional[GroupInfo]

class GroupDeleteResponse(BaseModel):
    pass

# --- Graph ---

class GraphSearchConfig(BaseModel):
    query: str
    top_k: Optional[int] = Field(default=1, ge=1, le=1000)
    min_k: float = Field(default=0.5, ge=0.0, le=1.0)
    top_n: Optional[int] = Field(default=1, ge=1)
    min_n: float = Field(default=0.5, ge=0.0, le=1.0)
    rerank_enabled: bool = True
    search_filter: Optional[dict] = None
    extra_groups: Optional[List[str]] = None
    max_hops: int = Field(default=1, ge=1, le=3)
    max_expansion_terms: int = Field(default=8, ge=0, le=30)
    auto_build: bool = False

class GroupGraphStatusResponse(BaseModel):
    graph: dict
    job: Optional[dict] = None

class GroupGraphBuildResponse(BaseModel):
    job: dict

class GroupGraphSearchResponse(BaseModel):
    nodes: List[dict]
    assets: List[dict]
    search_units: Optional[int] = None
    graph_metadata: dict = {}

# --- Content ---

class ContentUploadConfig(BaseModel):
    file_name: str
    file_contents: dict
    file_metadata: dict

class ContentUploadResponse(BaseModel):
    document_name: Optional[str] = None
    token_usage: Optional[int]

class Filter(BaseModel):
    should: Optional[List[dict]] = None
    must: Optional[List[dict]] = None
    must_not: Optional[List[dict]] = None

class SearchGroupConfig(BaseModel):
    prompt: str
    top_k: Optional[int] = Field(default=None, ge=1, le=1000)
    min_k: float = Field(default=0.0, ge=0.0, le=1.0)
    top_n: Optional[int] = Field(default=None, ge=1)
    min_n: float = Field(default=0.0, ge=0.0, le=1.0)
    rerank_enabled: bool = True
    search_filter: Optional[Filter] = None
    extra_groups: Optional[List[str]] = None

class CompletionUsage(BaseModel):
    completion_tokens: int
    prompt_tokens: int
    total_tokens: int
    usage_label: Optional[str] = None

class BaseNode(BaseModel):
    metadata: dict
    excluded_embed_metadata_keys: List[str] = []
    excluded_llm_metadata_keys: List[str] = []
    class_name: str

class TextNode(BaseNode):
    text: str
    text_template: str
    metadata_template: str

class NodeWithScore(BaseModel):
    node: BaseNode
    score: float

class TextNodeWithScore(NodeWithScore):
    node: TextNode

class Asset(BaseModel):
    id: int
    uuid: str
    document_id: int
    group_id: int
    mimetype: str
    data: str
    created: str
    description: str

class GroupSearchResponse(BaseModel):
    nodes: List[TextNodeWithScore]
    assets: List[Asset]
    search_units: Optional[int] = None
    metadata: dict = {}

class GraphBuildJob(BaseModel):
    job_id: Optional[str] = None
    group_name: Optional[str] = None
    state: str = "QUEUED"
    source: str = "none"
    progress: int = 0
    error: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

class GraphBuildResponse(GraphBuildJob):
    pass

class GraphState(BaseModel):
    status: str = "NOT_BUILT"
    source: str = "none"
    node_count: int = 0
    edge_count: int = 0
    top_entities: List[str] = []
    fallback_reason: Optional[str] = None
    error: Optional[str] = None
    built_at: Optional[int] = None
    updated_at: Optional[int] = None

class GraphStatusResponse(BaseModel):
    group_name: Optional[str] = None
    graph: GraphState = GraphState()
    job: Optional[GraphBuildJob] = None

class GraphSearchMetadata(BaseModel):
    status: str = "NOT_BUILT"
    source: str = "standard"
    expanded_terms: List[str] = []
    fallback_reason: Optional[str] = None
    max_hops: int = 1
    max_expansion_terms: int = 8
    job_id: Optional[str] = None
    latency_ms: Optional[int] = None

class GraphSearchResponse(GroupSearchResponse):
    graph_metadata: GraphSearchMetadata = GraphSearchMetadata()

class ContentListResponse(BaseModel):
    files: List[str]

class ContentDeleteResponse(BaseModel):
    pass

class ContentUpdateResponse(ContentUploadResponse):
    pass

# --- Group Auth ---

class GroupAuthCheckResponse(BaseModel):
    authorized: Optional[bool]
    master: Optional[bool]

class GroupAuthListResponse(BaseModel):
    indexes: List[GroupInfo]

# --- Model config ---

class AzureModelPartialBaseConfig(BaseModel):
    api_resource: str = "your-resource-or-endpoint"
    api_version: str = "2023-05-15"
    api_key: str = "your-controllers-key"
    api_deployment: str = "your-deployment-name"

class AzureModelBaseConfig(AzureModelPartialBaseConfig):
    api_model: str = "text-embedding-ada-002"

class AzureModelConfig(AzureModelBaseConfig):
    id: int

class CohereModelPartialBaseConfig(BaseModel):
    api_key: str = "your-api-key"

class CohereModelBaseConfig(CohereModelPartialBaseConfig):
    api_model: str = "rerank-multilingual-v2.0"

class CohereModelConfig(CohereModelBaseConfig):
    id: int

class GenericModelConfig(BaseModel):
    id: int
    provider_type: str
    config: Dict[str, Any]

class ModelCreateResponse(BaseModel):
    model: Optional[Union[AzureModelConfig, CohereModelConfig, GenericModelConfig]]

class ModelAboutResponse(ModelCreateResponse):
    pass

class ModelDeleteResponse(BaseModel):
    pass

# --- Agent chat / rerank / transform ---

class TextBlock(BaseModel):
    block_type: Literal["text"] = "text"
    text: str

class ImageBlock(BaseModel):
    block_type: Literal["image"] = "image"
    image: Optional[str] = None
    path: Optional[FilePath] = None
    url: Union[AnyUrl, str, None] = None
    image_mimetype: Optional[str] = None
    detail: Optional[str] = None

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    blocks: List[Union[TextBlock, ImageBlock]]
    additional_kwargs: dict = dict()
    metadata: dict = dict()

class ChatMessageRaw(BaseModel):
    id: str
    choices: List[dict]
    created: int
    model: str
    object: str
    system_fingerprint: Any
    usage: CompletionUsage

class ChatResponse(BaseModel):
    message: ChatMessage
    raw: ChatMessageRaw

class QueryModelParameters(BaseModel):
    max_reply_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None

class ChatAgentConfig(QueryModelParameters):
    history: List[ChatMessage]

class BaseAgentResponse(BaseModel):
    message: Optional[str] = None

class LLMAgentResponse(BaseAgentResponse):
    usage: List[CompletionUsage]

class ChatAgentResponse(LLMAgentResponse):
    chat_response: ChatResponse

class AgentChatResponse(BaseModel):
    agent_response: Optional[ChatAgentResponse]

class RelatedPrompt(BaseModel):
    label: str
    prompt: str
    llm_generated: bool = False

class RelatedPromptsAgentResponse(LLMAgentResponse):
    related_prompts: List[RelatedPrompt]

class RelatedPromptsAgentConfig(QueryModelParameters):
    llm_prompt: str
    llm_reply: str

class AgentRelatedPromptsResponse(BaseModel):
    agent_response: Optional[RelatedPromptsAgentResponse]

class TransformAgentResponse(LLMAgentResponse):
    new_prompt: Optional[str] = None

class TransformAgentConfig(QueryModelParameters):
    prompt: str
    history: List[ChatMessage]

class AgentTransformResponse(BaseModel):
    agent_response: Optional[TransformAgentResponse]

class RerankAgentResponse(BaseAgentResponse):
    ranked_nodes: List[TextNodeWithScore] = []
    search_units: int

class RerankAgentConfig(QueryModelParameters):
    prompt: str
    top_n: Optional[int] = Field(default=None, ge=1)
    min_n: float = Field(default=0.0, ge=0.0, le=1.0)
    nodes: List[TextNodeWithScore]

class AgentRerankResponse(BaseModel):
    agent_response: Optional[RerankAgentResponse]
