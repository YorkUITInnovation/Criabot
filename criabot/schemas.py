from pydantic import BaseModel

from criabot.database.bots.tables.bot_params import BotParametersModel, BotParametersBaseConfig
from criabot.database.bots.tables.bots import BotsModel


class InitializedAlreadyError(RuntimeError):
    """Can't initialize twice"""


class BotExistsError(RuntimeError):
    """Thrown when trying to create a bot that already exists"""


class BotNotFoundError(RuntimeError):
    """Thrown if trying to perform an action on a bot that doesn't exist"""


class BotCreateConfig(BotParametersBaseConfig):
    llm_model_id: int
    embedding_model_id: int
    rerank_model_id: int


class AboutBot(BaseModel):
    info: BotsModel
    params: BotParametersModel


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
