import asyncio
import secrets
from asyncio import AbstractEventLoop
from typing import Optional, Tuple

import aioredis
from CriadexSDK import CriadexSDK
from CriadexSDK.routers.auth import AuthCreateRoute
from CriadexSDK.routers.auth.create import AuthCreateConfig
from CriadexSDK.routers.group_auth import GroupAuthCreateRoute
from CriadexSDK.routers.groups import GroupDeleteRoute, GroupCreateRoute
from CriadexSDK.routers.groups.about import GroupInfo
from CriadexSDK.routers.groups.create import PartialGroupConfig, IndexTypes
from aiomysql import Pool
from aioredis import ConnectionPool
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from criabot.bot.chat.chat import Chat
from criabot.schemas import MySQLCredentials, RedisCredentials, CriadexCredentials, BotExistsError, \
    BotCreateConfig, BotNotFoundError, AboutBot
from .bot.bot import Bot
from .bot.schemas import ChatNotFoundError
from .cache.api import BotCacheAPI
from .cache.objects.chats import ChatModel
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
        self._criadex: CriadexSDK = CriadexSDK(
            api_base=self._criadex_credentials.api_base,
            error_stacktrace=criadex_stacktrace
        )

        # Database
        self._mysql_engine: Optional[Pool] = None
        self._mysql_api: Optional[BotDatabaseAPI] = None

        # Cache
        self._redis_pool: Optional[ConnectionPool] = None
        self._redis_api: Optional[BotCacheAPI] = None

        # Other
        try:
            self._loop: AbstractEventLoop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop: AbstractEventLoop = asyncio.get_event_loop()

        self._already_initialized: bool = False

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
        self._redis_api: BotCacheAPI = BotCacheAPI(pool=self._redis_pool)

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

    async def create(self, name: str, config: BotCreateConfig) -> AuthCreateRoute.Response:
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
        new_auth: AuthCreateRoute.Response = await self._create_new_bot_auth()

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

    async def get(self, name: str) -> Bot:
        """
        Retrieve an existing bot

        :param name: The name of the bot
        :return: Instance of the bot

        """

        # Confirm the bot exists cuz some people are WILD
        if not await self.exists(name):
            raise BotNotFoundError()

        # Create a bot (light-weight operation)
        return Bot(
            name=name,
            criadex=self._criadex,
            bot_cache=self._redis_api
        )

    async def get_bot_chat(self, bot_name: str, chat_id: str) -> Chat:
        """
        Get a bot chat given its ID

        :param chat_id: The chat ID
        :param bot_name: Bot name
        :return: The chat
        :raises ChatNotFoundError: Raised if the chat does not exist

        """

        chat_model: ChatModel = await self._redis_api.chats.get(chat_id=chat_id)
        bot_parameters: AboutBot = await self.about(name=bot_name)
        bot: Bot = await self.get(name=bot_name)
        group_info: GroupInfo = await bot.retrieve_group_info()

        # If the chat DNE
        if chat_model is None:
            raise ChatNotFoundError(chat_id=chat_id)

        # Create light-weight chat
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

        if bot_id is None:
            raise BotNotFoundError()

        await self._mysql_api.bot_params.update(bot_id=bot_id, config=params)

    async def _create_new_bot_auth(self) -> AuthCreateRoute.Response:
        """
        Create a new authentication token for use with the bot

        :return: The new Criadex API key

        """

        result: AuthCreateRoute.Response = await self._criadex.auth.create(
            api_key=(secrets.token_urlsafe(32)),
            create_config=AuthCreateConfig(
                master=False
            )
        )

        return result.verify()

    async def _create_new_bot_groups(
            self,
            bot_name: str,
            bot_config: BotCreateConfig,
            bot_api_key: str
    ) -> Tuple[GroupCreateRoute.Response, GroupCreateRoute.Response]:
        """
        Create the necessary indexes for the bot

        :param bot_name: The name of the bot
        :param bot_config: The config (model definitions pretty much)
        :param bot_api_key: The API key to authenticate on the indexes
        :return: The indices, in the order of [DOCUMENT, QUESTION, CACHE]

        """

        async def create_group(index_type: IndexTypes) -> GroupCreateRoute.Response:
            group_name: str = bot_name + Bot.INDEX_SUFFIX[index_type]

            # Create the index
            new_group: GroupCreateRoute.Response = await self._create_new_bot_group(
                group_name=group_name,
                group_config=PartialGroupConfig(
                    type=index_type,
                    llm_model_id=bot_config.llm_model_id,
                    embedding_model_id=bot_config.embedding_model_id,
                    rerank_model_id=bot_config.rerank_model_id
                )
            )

            # Authenticate the token on the index
            await self._create_new_bot_auth_group(
                group_name=group_name,
                bot_api_key=bot_api_key
            )

            # Return the index
            return new_group

        return (
            await create_group("QUESTION"),
            await create_group("DOCUMENT"),
            # await create_group("CACHE")
        )

    async def _create_new_bot_group(
            self,
            group_name: str,
            group_config: PartialGroupConfig
    ) -> GroupCreateRoute.Response:
        """
        Create a new Bot Index for a new Bot

        :param group_name: The name of the index
        :param group_config: Partial config for the SDK
        :return: Criadex API Response

        """

        result: GroupCreateRoute.Response = await self._criadex.manage.create(
            group_name=group_name,
            group_config=group_config
        )

        return result.verify()

    async def _create_new_bot_auth_group(
            self,
            group_name: str,
            bot_api_key: str
    ) -> GroupAuthCreateRoute.Response:
        """
        Create a new Index Authorization with the newly created API key

        :param group_name: The name of the index to add the key to
        :param bot_api_key: The key to add
        :return: Criadex API Response
        :raises CriadexError: If request fails

        """

        result: GroupAuthCreateRoute.Response = await self._criadex.group_auth.create(
            group_name=group_name,
            api_key=bot_api_key
        )

        return result.verify()

    @property
    def mysql_api(self) -> BotDatabaseAPI:
        return self._mysql_api

    @property
    def redis_api(self) -> BotCacheAPI:
        return self._redis_api

    @property
    def criadex(self) -> CriadexSDK:
        return self._criadex
