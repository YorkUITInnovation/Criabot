from typing import List, Optional, Dict
from pydantic import BaseModel, Field

from criabot.database.bots.tables.bot_params import BotParametersModel, BotParametersBaseConfig
from criabot.database.bots.tables.bots import BotsModel


class InitializedAlreadyError(RuntimeError):
    """Can't initialize twice"""


class BotExistsError(RuntimeError):
    """Thrown when trying to create a bot that already exists"""


class BotNotFoundError(RuntimeError):
    """Thrown if trying to perform an action on a bot that doesn't exist"""


class CircularDependencyError(RuntimeError):
    """Thrown when trying to create a circular parent-child relationship"""


class ParentNotFoundError(RuntimeError):
    """Thrown when a specified parent bot doesn't exist"""


class InvalidModelsError(RuntimeError):
    """Thrown when specified model IDs are invalid and don't exist"""


class BotCreateConfig(BotParametersBaseConfig):
    llm_model_id: int
    embedding_model_id: int
    rerank_model_id: int
    use_knowledge_graph: bool = True
    requires_documents: bool = True
    parent_bot_names: List[str] = Field(default_factory=list)
    parent_priorities: Optional[Dict[str, int]] = Field(default=None)


class BotUpdateConfig(BotParametersBaseConfig):
    """Configuration for updating bot parameters and parent relationships"""
    parent_bot_names: Optional[List[str]] = Field(default=None)
    parent_priorities: Optional[Dict[str, int]] = Field(default=None)


class AboutBot(BaseModel):
    info: BotsModel
    params: BotParametersModel
    parent_bot_names: List[str] = Field(default_factory=list)
    children: List[str] = Field(default_factory=list)
    effective_config: BotParametersModel
    # Optionally include the active bot API key for admin/master requests only
    bot_api_key: Optional[str] = None


class CriadexCredentials(BaseModel):
    """
    Credentials for Criadex SDK

    """

    api_base: str
    api_key: str  # Must be a master key
    master_api_key: str


class MySQLCredentials(BaseModel):
    """
    Credentials for accessing the MySQL Database

    """

    host: str
    port: int
    username: str
    password: str
    database: str


class RedisCredentials(BaseModel):
    """
    Credentials for accessing the Redis Memcache

    """

    host: str
    port: int
    username: str
    password: str
