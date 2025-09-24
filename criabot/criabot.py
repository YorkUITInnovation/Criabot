import asyncio
import secrets
from asyncio import AbstractEventLoop
from typing import Optional, Tuple

from redis import asyncio as aioredis
from CriadexSDK.ragflow_sdk import RAGFlowSDK
from CriadexSDK.ragflow_schemas import AuthCreateConfig  # Use new schemas if needed
from aiomysql import Pool
from redis.asyncio import ConnectionPool
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from criabot.schemas import (
    MySQLCredentials,
    RedisCredentials,
    CriadexCredentials,
    BotExistsError,
    BotCreateConfig,
    BotNotFoundError,
    AboutBot
)
from .bot.schemas import ChatNotFoundError
from .database.bots.bots import BotDatabaseAPI
from .database.bots.tables.bot_params import BotParametersModel, BotParametersConfig, BotParametersBaseConfig
from .database.bots.tables.bots import BotsModel, BotsConfig
from .schemas import InitializedAlreadyError


class Criabot:
    """
    Manage Cria bot

    """

    def __init__(
            self,
            criadex_credentials: CriadexCredentials,
            mysql_credentials: MySQLCredentials,
            redis_credentials: RedisCredentials,
            criadex_stacktrace: bool = False
    ):

        # Credentials
        self._mysql_credentials: MySQLCredentials = mysql_credentials
        self._redis_credentials: RedisCredentials = redis_credentials
        self._criadex_credentials: CriadexCredentials = criadex_credentials

        # Criadex SDK
        self._criadex: RAGFlowSDK = RAGFlowSDK(
            api_base=self._criadex_credentials.api_base,
            error_stacktrace=criadex_stacktrace
        )

        # Database
        self._mysql_engine = None
        self._mysql_api = None

        # Cache
        self._redis_pool = None
        self._redis_api = None

        # Other
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        self._already_initialized = False

    async def initialize(self) -> None:
        """
        Initialize the various databases, caches, and APIs

        :return: None

        """

        if self._already_initialized:
            raise InitializedAlreadyError()

        # Criadex Startup
        await self._criadex.authenticate(self._criadex_credentials.api_key)

        # SQL DB Startup
        self._mysql_engine: AsyncEngine = await self._create_mysql_engine()

        # Redis DB Startup
        self._redis_pool: ConnectionPool = aioredis.ConnectionPool(
            host=self._redis_credentials.host,
            port=self._redis_credentials.port,
            username=self._redis_credentials.username,
            password=self._redis_credentials.password
        )

        # SQL DB API Startup
        self._mysql_api: BotDatabaseAPI = BotDatabaseAPI(engine=self._mysql_engine)
        await self._mysql_api.initialize()

        # Redis API Startup
        from .cache.api import BotCacheAPI
        self._redis_api = BotCacheAPI(pool=self._redis_pool)

    async def _create_mysql_engine(self) -> AsyncEngine:
        """
        Create the MYSQL pool & database if not found
        :return: The pool

        """

        init_engine: AsyncEngine = create_async_engine(
            URL.create(
                drivername="mysql+aiomysql",
                host=self._mysql_credentials.host,
                port=self._mysql_credentials.port,
                username=self._mysql_credentials.username,
                password=self._mysql_credentials.password,
            )
        )

        async with init_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS "
                    f"{self._mysql_credentials.database}"
                )
            )

        self._mysql_engine = create_async_engine(
            URL.create(
                drivername="mysql+aiomysql",
                host=self._mysql_credentials.host,
                port=self._mysql_credentials.port,
                username=self._mysql_credentials.username,
                password=self._mysql_credentials.password,
                database=self._mysql_credentials.database
            )
        )

        return self._mysql_engine

    async def create(self, name: str, config: BotCreateConfig):
        """
        Create a bot, including all the required indexes

        :param name: The name of the bot
        :param config: Its config
        :return: An auth token generated for end-users to interact with this bot
        :raises CriadexError: If anything goes wrong

        """

        # Check if the bot already exists
        if await self.exists(name):
            raise BotExistsError()

        # Make a new access token
        new_auth = await self._create_new_bot_auth()

        # Create the indexes & authenticate on them
        await self._create_new_bot_groups(
            bot_name=name,
            bot_api_key=new_auth.api_key,
            bot_config=config
        )

        # Add the bot to MySQL
        bot_id: int = await self._mysql_api.bots.insert(
            BotsConfig(
                name=name
            )
        )

        # Store the parameters
        await self._mysql_api.bot_params.insert(
            config=BotParametersConfig(
                bot_id=bot_id,
                **config.model_dump()
            )
        )

        # Return the config
        return new_auth

    async def get_id(self, name: str) -> int:
        """
        Get a bot's ID if it exists

        """

        bot_id: Optional[int] = await self._mysql_api.bots.retrieve_id(name=name)

        if not bot_id:
            raise BotNotFoundError()

        return bot_id

    async def exists(self, *names: str) -> bool:
        """
        Check if a bot exists given its name

        :param names: The name of the bots to check
        :return: Whether it exists

        """

        return await self._mysql_api.bots.exists(*names)

    async def delete(self, name: str) -> None:
        """
        Delete a bot, including its indexes (which will auto-delete the authorizations)

        :param name: The name of the bot
        :return: None
        :raises CriadexError: If the bloody monstrosity fails

        """

        # Bot has to exist to delete it
        bot_id: Optional[int] = await self.get_id(name=name)
        if bot_id is None:
            raise BotNotFoundError()

        # Get the bot to delete it
        from .bot.bot import Bot
        bot: Bot = await self.get(name=name)

        # Get index names
        group_names = (
            bot.group_name(index_type="QUESTION"),
            bot.group_name(index_type="DOCUMENT"),
            # bot.group_name(index_type="CACHE")
        )

        # Delete the indexes
        for group_name in group_names:
            response: GroupDeleteRoute.Response = await self._criadex.manage.delete(
                group_name=group_name
            )

            response.verify()

        # Delete the bot params.py
        await self._mysql_api.bot_params.delete(bot_id=bot_id)

        # Delete from MySQL
        await self._mysql_api.bots.delete(name=name)

    async def about(self, name: str) -> AboutBot:
        """
        Retrieve the Bot's config

        :param name: Name of the bot
        :return: Information about the bot stored in MySQL

        """

        bots_model: BotsModel = await self._mysql_api.bots.retrieve(name=name)

        if bots_model is None:
            raise BotNotFoundError()

        params_model: BotParametersModel = await self._mysql_api.bot_params.retrieve(bot_id=bots_model.id)

        # Build an about-me
        return AboutBot(
            info=bots_model,
            params=params_model
        )

    async def get(self, name: str):
        """
        Retrieve an existing bot

        :param name: The name of the bot
        :return: Instance of the bot

        """

        # Confirm the bot exists cuz some people are WILD
        if not await self.exists(name):
            raise BotNotFoundError()

        # Create a bot (light-weight operation)
        from .bot.bot import Bot
        return Bot(
            name=name,
            criadex=self._criadex,
            bot_cache=self._redis_api
        )

    async def get_bot_chat(self, bot_name: str, chat_id: str):
        """
        Get a bot chat given its ID

        :param chat_id: The chat ID
        :param bot_name: Bot name
        :return: The chat
        :raises ChatNotFoundError: Raised if the chat does not exist

        """

        from .cache.objects.chats import ChatModel
        chat_model: ChatModel = await self._redis_api.chats.get(chat_id=chat_id)
        bot_parameters: AboutBot = await self.about(name=bot_name)
        bot = await self.get(name=bot_name)
        group_info = await bot.retrieve_group_info()

        # If the chat DNE
        if chat_model is None:
            raise ChatNotFoundError(chat_id=chat_id)

        # Create light-weight chat
        from criabot.bot.chat.chat import Chat
        return Chat(
            bot=bot,
            llm_model_id=group_info.llm_model_id,
            rerank_model_id=group_info.rerank_model_id,
            chat_model=chat_model,
            chat_id=chat_id,
            bot_parameters=bot_parameters.params
        )

    async def end_bot_chat(self, chat_id: str) -> None:
        """
        End a chat forcibly

        :return: None

        """

        if not await self._redis_api.chats.exists(chat_id=chat_id):
            raise ChatNotFoundError(chat_id=chat_id)

        await self._redis_api.chats.delete(chat_id=chat_id)

    async def update_parameters(self, name: str, params: BotParametersBaseConfig) -> None:

        bot_id: Optional[int] = await self._mysql_api.bots.retrieve_id(name=name)

    async def _create_new_bot_auth(self):
        """
        Create a new authentication token for use with the bot

        :return: The new Criadex API key

        """

        result = await self._criadex.auth.create(
            api_key=(secrets.token_urlsafe(32)),
            create_config=AuthCreateConfig(
                master=False
            )
        )
        # Optionally: check response for success or error
        return result

    async def _create_new_bot_groups(
        self,
        bot_name: str,
        bot_config: BotCreateConfig,
        bot_api_key: str
    ):
        """
        Create the necessary indexes for the bot

        :param bot_name: The name of the bot
        :param bot_config: The config (model definitions pretty much)
        :param bot_api_key: The API key to authenticate on the indexes
        :return: The indices, in the order of [DOCUMENT, QUESTION, CACHE]

        """


        async def create_group(index_type):
            from .bot.bot import Bot
            group_name = bot_name + Bot.INDEX_SUFFIX[index_type]
            new_group = await self._create_new_bot_group(
                group_name=group_name,
                group_config={
                    "type": index_type,
                    "llm_model_id": bot_config.llm_model_id,
                    "embedding_model_id": bot_config.embedding_model_id,
                    "rerank_model_id": bot_config.rerank_model_id
                }
            )
            await self._create_new_bot_auth_group(
                group_name=group_name,
                bot_api_key=bot_api_key
            )
            return new_group

        return (
            await create_group("QUESTION"),
            await create_group("DOCUMENT"),
            # await create_group("CACHE")
        )

    async def _create_new_bot_group(
            self,
            group_name: str,
            group_config: dict
    ):
        """
        Create a new Bot Index for a new Bot

        :param group_name: The name of the index
        :param group_config: Partial config for the SDK
        :return: RAGFlow API Response

        """
        result = await self._criadex.manage.create(
            group_name=group_name,
            group_config=group_config
        )
        # Optionally: check response for success or error
        return result

    async def _create_new_bot_auth_group(
            self,
            group_name: str,
            bot_api_key: str
    ):
        """
        Create a new Index Authorization with the newly created API key

        :param group_name: The name of the index to add the key to
        :param bot_api_key: The key to add
        :return: RAGFlow API Response
        :raises Exception: If request fails

        """
        result = await self._criadex.group_auth.create(
            group_name=group_name,
            api_key=bot_api_key
        )
        # Optionally: check response for success or error
        return result

    @property
    def mysql_api(self) -> BotDatabaseAPI:
        return self._mysql_api

    @property
    def redis_api(self):
        return self._redis_api

    @property
    def criadex(self):
        return self._criadex
