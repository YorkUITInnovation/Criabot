import asyncio
import secrets
from typing import Optional, Dict, List

from redis import asyncio as aioredis
from CriadexSDK.ragflow_sdk import RAGFlowSDK
from CriadexSDK.ragflow_schemas import AuthCreateConfig, GroupDeleteResponse
from aiomysql import Pool
from redis.asyncio import ConnectionPool
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy.exc import IntegrityError

from criabot.database.table import BaseTable

from criabot.schemas import (
    MySQLCredentials,
    RedisCredentials,
    CriadexCredentials,
    BotExistsError,
    BotCreateConfig,
    BotNotFoundError,
    AboutBot,
    ParentNotFoundError,
    CircularDependencyError,
    InvalidModelsError,
)
from criabot.bot.inheritance import check_circular_dependency, merge_configurations
from .bot.schemas import ChatNotFoundError
from .database.bots.bots import BotDatabaseAPI
from .database.bots.tables.bot_params import BotParametersModel, BotParametersConfig, BotParametersBaseConfig
from .database.bots.tables.bots import BotsModel, BotsConfig
from .schemas import InitializedAlreadyError

import logging
logger = logging.getLogger(__name__)

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

        self._already_initialized = False

    async def initialize(self) -> None:
        """
        Initialize the various databases, caches, and APIs

        :return: None

        """

        if self._already_initialized:
            raise InitializedAlreadyError()

        # Criadex Startup
        # Authenticate with the initial master key
        self._criadex.authenticate(self._criadex_credentials.master_api_key)

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

        created_groups: List[str] = []
        bot_id: Optional[int] = None
        new_auth: Optional[dict] = None

        try:
            # Step 1: create API key for this bot
            new_auth = await self._create_new_bot_auth()
            api_key: str = new_auth["api_key"]

            # Step 2: create the bot's own groups and authorize its key on them
            from .bot.bot import Bot

            question_group, document_group = await self._create_new_bot_groups(
                bot_name=name,
                bot_api_key=api_key,
                bot_config=config,
            )
            def extract_group_name(group_response, expected_name):
                if isinstance(group_response, dict):
                    return group_response.get("group_name") or group_response.get("name") or expected_name
                return getattr(group_response, "group_name", None) or getattr(group_response, "name", None) or expected_name

            def was_group_created(group_response) -> bool:
                if isinstance(group_response, dict):
                    return bool(group_response.get("created", False))
                return bool(getattr(group_response, "created", False))

            from .bot.bot import Bot
            doc_group_name = Bot.bot_group_name(name, "DOCUMENT")
            question_group_name = Bot.bot_group_name(name, "QUESTION")

            if was_group_created(document_group):
                created_groups.append(extract_group_name(document_group, doc_group_name))
            if was_group_created(question_group):
                created_groups.append(extract_group_name(question_group, question_group_name))

            # Step 3: persist bot and parameters in MySQL
            bot_id = await self._mysql_api.bots.insert(
                BotsConfig(
                    name=name,
                )
            )

            await self._mysql_api.bot_params.insert(
                config=BotParametersConfig(
                    bot_id=bot_id,
                    **config.model_dump(),
                )
            )

            # Persist model IDs so chat creation doesn't depend on Criadex
            # group metadata being available at request time.
            from criabot.database.bots.tables.bot_models import BotModelConfig
            await self._mysql_api.bot_models.insert(
                config=BotModelConfig(
                    bot_id=bot_id,
                    llm_model_id=config.llm_model_id,
                    rerank_model_id=config.rerank_model_id,
                )
            )

            # Persist the created API key for this bot so parent/child flows can find it
            from criabot.database.bots.tables.bot_api_keys import BotApiKeyConfig
            import inspect
            insert_candidate = self._mysql_api.bot_api_keys.insert(
                BotApiKeyConfig(bot_id=bot_id, api_key=api_key)
            )
            if inspect.isawaitable(insert_candidate):
                await insert_candidate

            # Step 4: if parents specified, validate and create parent-child relationships
            if config.parent_bot_names:
                parent_ids_by_name = await self._get_parent_ids_by_name(
                    parent_names=config.parent_bot_names
                )

                # Prevent circular dependencies in the parent graph
                parent_ids = list(parent_ids_by_name.values())
                has_cycle = await check_circular_dependency(
                    bot_parents_api=self._mysql_api.bot_parents,
                    child_bot_id=bot_id,
                    parent_bot_ids=parent_ids,
                )
                if has_cycle:
                    raise CircularDependencyError(
                        "Adding the specified parents would create a circular dependency."
                    )

                await self._assign_parents(
                    child_bot_id=bot_id,
                    parent_ids_by_name=parent_ids_by_name,
                    parent_priorities=config.parent_priorities or {},
                )

                # Authorize child API key on all parent groups (read access to parents)
                await self._authorize_child_on_parent_groups(
                    child_api_key=api_key,
                    parent_names=list(parent_ids_by_name.keys()),
                )

                # Authorize each parent's API key on the child's groups (parent oversight)
                await self._authorize_parents_on_child_groups(
                    child_name=name,
                    parent_ids_by_name=parent_ids_by_name,
                )

            # Success
            return new_auth

        except BotExistsError:
            # Re-raise without cleanup – caller expects this
            raise
        except IntegrityError:
            # A concurrent create may have inserted this bot first; treat as
            # a duplicate and rollback any resources created for this attempt.
            await self._rollback_failed_bot_creation(
                name=name,
                created_groups=created_groups,
                new_auth=new_auth,
                bot_id=bot_id,
            )
            raise BotExistsError()
        except Exception as e:
            # Check if this is a Criadex API error with model validation failure
            error_str = str(e)
            if "INVALID_MODEL" in error_str or "does not exist" in error_str:
                await self._rollback_failed_bot_creation(
                    name=name,
                    created_groups=created_groups,
                    new_auth=new_auth,
                    bot_id=bot_id,
                )
                raise InvalidModelsError(error_str)
            
            # Best-effort rollback of created external resources and DB rows
            await self._rollback_failed_bot_creation(
                name=name,
                created_groups=created_groups,
                new_auth=new_auth,
                bot_id=bot_id,
            )
            raise

    async def _rollback_failed_bot_creation(
        self,
        name: str,
        created_groups: List[str],
        new_auth: Optional[dict],
        bot_id: Optional[int],
    ) -> None:
        """
        Best-effort rollback helper for failed bot creation attempts.
        Cleans up external Criadex resources and partially created DB rows.
        """
        # 1) delete groups in Criadex
        for group_name in created_groups:
            try:
                await self._criadex.manage.delete(group_name=group_name)
            except Exception:
                # Ignore cleanup errors
                pass

        # 2) delete API key in Criadex
        if new_auth is not None:
            try:
                api_preview = None
                if isinstance(new_auth, dict) and 'api_key' in new_auth:
                    api_preview = new_auth['api_key'][:20] + '...'
                logger.debug("Rollback: attempting to delete API key %s", api_preview)
            except Exception:
                # best-effort; ignore if new_auth malformed
                pass

            try:
                import inspect
                delete_candidate = self._criadex.auth.delete(api_key=new_auth["api_key"])
                if inspect.isawaitable(delete_candidate):
                    result = await delete_candidate
                else:
                    result = delete_candidate
                try:
                    # Mask any api_key in the result when logging
                    log_result = result.copy() if isinstance(result, dict) else result
                    if isinstance(log_result, dict) and 'api_key' in log_result:
                        log_result['api_key'] = str(log_result['api_key'])[:6] + '...'
                    logger.debug("Rollback: auth.delete result: %s", log_result)
                except Exception:
                    pass
            except Exception as e:
                # Log the exception so we can see what failed during rollback
                logger.debug("Rollback: auth.delete raised: %s", e, exc_info=True)
                # continue - rollback should be best-effort and not crash the handler
                pass

        # 3) delete partially created DB rows
        if bot_id is not None:
            try:
                await self._mysql_api.bot_params.delete(bot_id=bot_id)
            except Exception:
                pass
            try:
                await self._mysql_api.bots.delete(name=name)
            except Exception:
                pass

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

        deleted_groups: List[str] = []

        try:
            # Delete the indexes first (external dependency)
            for group_name in group_names:
                try:
                    await self._criadex.manage.delete(group_name=group_name)
                    deleted_groups.append(group_name)
                except Exception:
                    # Group might already be gone – continue
                    continue

            # Delete from MySQL (parameters then bot)
            await self._mysql_api.bot_params.delete(bot_id=bot_id)
            await self._mysql_api.bots.delete(name=name)

        except Exception as e:
            # At this point groups may already be removed; surface a clear error so callers
            # can detect partial deletion and retry or alert.
            raise RuntimeError(
                f"Failed to delete bot '{name}' cleanly; some resources may remain."
            ) from e

    async def about(self, name: str) -> AboutBot:
        """
        Retrieve the Bot's config

        :param name: Name of the bot
        :return: Information about the bot stored in MySQL

        """

        bots_model: BotsModel = await self._mysql_api.bots.retrieve(name=name)

        if bots_model is None:
            raise BotNotFoundError()

        params_model: BotParametersModel = await self._mysql_api.bot_params.retrieve(
            bot_id=bots_model.id
        )

        # Lookup parent and child relationships for this bot
        parent_ids = await self._mysql_api.bot_parents.get_parent_ids(
            child_bot_id=bots_model.id
        )
        child_ids = await self._mysql_api.bot_parents.get_child_ids(
            parent_bot_id=bots_model.id
        )

        parent_names = []
        parent_param_configs: List[BotParametersModel] = []
        parent_priorities: Dict[int, int] = {}

        # Gather parent names and configs (for inheritance) - batch fetch to avoid N+1 queries
        if parent_ids:
            from criabot.database.bots.tables.bot_parents import BotParentsModel

            relationships = await self._mysql_api.bot_parents.get_by_child(
                child_bot_id=bots_model.id
            )
            rel_by_parent_id: Dict[int, BotParentsModel] = {
                rel.parent_bot_id: rel for rel in relationships
            }

            # Batch fetch all parent bots and their parameters in single queries
            parent_models = await self._mysql_api.bots.retrieve_by_ids(bot_ids=parent_ids)
            parent_models_by_id: Dict[int, BotsModel] = {
                model.id: model for model in parent_models
            }

            parent_params_list = await self._mysql_api.bot_params.retrieve_by_bot_ids(
                bot_ids=parent_ids
            )
            parent_params_by_bot_id: Dict[int, BotParametersModel] = {
                params.bot_id: params for params in parent_params_list
            }

            for parent_id in parent_ids:
                parent_model = parent_models_by_id.get(parent_id)
                if parent_model is None:
                    continue

                parent_names.append(parent_model.name)

                parent_params = parent_params_by_bot_id.get(parent_id)
                if parent_params is not None:
                    parent_param_configs.append(parent_params)

                    rel = rel_by_parent_id.get(parent_id)
                    if rel is not None:
                        parent_priorities[parent_params.bot_id] = rel.priority

        # Batch fetch child bots to avoid N+1 queries
        child_names = []
        if child_ids:
            child_models = await self._mysql_api.bots.retrieve_by_ids(bot_ids=child_ids)
            child_names = [model.name for model in child_models]

        # Build effective configuration: if no parents, use own params.
        # If parents exist, merge their configs and let child override all fields.
        if parent_param_configs:
            merged_parents = merge_configurations(
                configs=parent_param_configs,
                priorities=parent_priorities or None,
            )
            # Merge: start with merged parent config, then apply child overrides
            # This shows what the bot actually uses (inherited + overridden)
            effective_config = BotParametersModel(
                id=params_model.id,
                bot_id=params_model.bot_id,
                # Child's stored values are what it actually uses (they override parents)
                max_input_tokens=params_model.max_input_tokens,
                max_reply_tokens=params_model.max_reply_tokens,
                temperature=params_model.temperature,
                top_p=params_model.top_p,
                top_k=params_model.top_k,
                min_k=params_model.min_k,
                top_n=params_model.top_n,
                min_n=params_model.min_n,
                llm_generate_related_prompts=params_model.llm_generate_related_prompts,
                no_context_message=params_model.no_context_message,
                no_context_use_message=params_model.no_context_use_message,
                no_context_llm_guess=params_model.no_context_llm_guess,
                # system_message: child override if present, otherwise inherit from merged parents
                system_message=params_model.system_message or merged_parents.system_message,
            )
        else:
            effective_config = params_model

        # Look up the bot's active API key (if any) and include it in the about response
        import inspect
        bot_key_candidate = self._mysql_api.bot_api_keys.get_active_by_bot(bot_id=bots_model.id)
        if inspect.isawaitable(bot_key_candidate):
            bot_key_model = await bot_key_candidate
        else:
            bot_key_model = bot_key_candidate
        # Ensure the returned API key is a string (tests may return MagicMock objects)
        bot_api_key = None
        if bot_key_model is not None:
            candidate_key = getattr(bot_key_model, "api_key", None)
            if isinstance(candidate_key, str):
                bot_api_key = candidate_key
            else:
                # Best-effort coerce to string, otherwise treat as not present
                try:
                    bot_api_key = str(candidate_key)
                except Exception:
                    bot_api_key = None

        # Build an about-me including hierarchy info, effective config, and optionally the API key
        return AboutBot(
            info=bots_model,
            params=params_model,
            parent_bot_names=parent_names,
            children=child_names,
            effective_config=effective_config,
            bot_api_key=bot_api_key,
        )

    async def get(self, name: str) -> "Bot":
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

    async def get_bot_chat(self, bot_name: str, chat_id: str) -> "Chat":
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

        # If the chat DNE
        if chat_model is None:
            raise ChatNotFoundError(chat_id=chat_id)

        bot_id = bot_parameters.info.id
        model_config = await self._mysql_api.bot_models.retrieve_by_bot_id(bot_id=bot_id)
        if model_config is not None:
            llm_model_id = model_config.llm_model_id
            rerank_model_id = model_config.rerank_model_id
        else:
            # Backward compatibility: fall back to Criadex group metadata.
            group_info = await bot.retrieve_group_info()
            llm_model_id = group_info["info"]["llm_model_id"]
            rerank_model_id = group_info["info"]["rerank_model_id"]

        # Create light-weight chat
        from criabot.bot.chat.chat import Chat
        return Chat(
            bot=bot,
            llm_model_id=llm_model_id,
            rerank_model_id=rerank_model_id,
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
        """Update bot parameters"""
        bot_id: Optional[int] = await self._mysql_api.bots.retrieve_id(name=name)
        if bot_id is None:
            raise BotNotFoundError()
        
        await self._mysql_api.bot_params.update(bot_id=bot_id, config=params)

    async def update_parent_relationships(
        self,
        child_name: str,
        new_parent_names: List[str],
        parent_priorities: Optional[Dict[str, int]] = None,
    ) -> None:
        """
        Update parent relationships for a bot.
        Removes old parent relationships and authorizations, then adds new ones.
        """
        bot_id = await self.get_id(name=child_name)
        
        # Get current parent relationships
        current_parent_ids = await self._mysql_api.bot_parents.get_parent_ids(
            child_bot_id=bot_id
        )
        current_parent_names = []
        for parent_id in current_parent_ids:
            parent_model = await self._mysql_api.bots.retrieve_by_id(parent_id)
            if parent_model is not None:
                current_parent_names.append(parent_model.name)
        
        # Determine which relationships to remove and add
        current_set = set(current_parent_names)
        new_set = set(new_parent_names)
        to_remove = current_set - new_set
        to_add = new_set - current_set
        
        # Get child's API key
        import inspect
        child_key_candidate = self._mysql_api.bot_api_keys.get_active_by_bot(bot_id=bot_id)
        if inspect.isawaitable(child_key_candidate):
            child_key = await child_key_candidate
        else:
            child_key = child_key_candidate
        if child_key is None:
            raise RuntimeError(f"Child bot '{child_name}' has no active API key")
        child_api_key = child_key.api_key
        
        # Remove old parent relationships and authorizations
        for parent_name in to_remove:
            parent_id = await self._mysql_api.bots.retrieve_id(name=parent_name)
            if parent_id is None:
                continue
            
            # Remove from BotParents table
            await self._mysql_api.bot_parents.delete(
                child_bot_id=bot_id,
                parent_bot_id=parent_id
            )
            
            # Revoke child's access to parent groups
            from .bot.bot import Bot
            for index_type in ("DOCUMENT", "QUESTION"):
                group_name = Bot.bot_group_name(parent_name, index_type)
                try:
                    await self._criadex.group_auth.delete(
                        group_name=group_name,
                        api_key=child_api_key
                    )
                except Exception:
                    # Ignore if authorization doesn't exist
                    pass
            
            # Revoke parent's access to child groups
            import inspect
            parent_key_candidate = self._mysql_api.bot_api_keys.get_active_by_bot(
                bot_id=parent_id
            )
            if inspect.isawaitable(parent_key_candidate):
                parent_key = await parent_key_candidate
            else:
                parent_key = parent_key_candidate
            if parent_key is not None:
                for index_type in ("DOCUMENT", "QUESTION"):
                    group_name = Bot.bot_group_name(child_name, index_type)
                    try:
                        await self._criadex.group_auth.delete(
                            group_name=group_name,
                            api_key=parent_key.api_key
                        )
                    except Exception:
                        # Ignore if authorization doesn't exist
                        pass
        
        # Add new parent relationships and authorizations
        if to_add:
            parent_ids_by_name = await self._get_parent_ids_by_name(
                parent_names=list(to_add)
            )
            
            # Check for circular dependencies
            parent_ids = list(parent_ids_by_name.values())
            has_cycle = await check_circular_dependency(
                bot_parents_api=self._mysql_api.bot_parents,
                child_bot_id=bot_id,
                parent_bot_ids=parent_ids,
            )
            if has_cycle:
                raise CircularDependencyError(
                    "Adding the specified parents would create a circular dependency."
                )
            
            # Create new relationships
            priorities = parent_priorities or {}
            await self._assign_parents(
                child_bot_id=bot_id,
                parent_ids_by_name=parent_ids_by_name,
                parent_priorities=priorities,
            )
            
            # Grant child access to new parent groups
            await self._authorize_child_on_parent_groups(
                child_api_key=child_api_key,
                parent_names=list(parent_ids_by_name.keys()),
            )
            
            # Grant new parents access to child groups
            await self._authorize_parents_on_child_groups(
                child_name=child_name,
                parent_ids_by_name=parent_ids_by_name,
            )

    async def _create_new_bot_auth(self):
        """
        Create a new authentication token for use with the bot

        :return: The new Criadex API key

        """
        api_key = secrets.token_urlsafe(32)
        try:
            logger.debug("Creating bot auth (masked): %s", api_key[:6] + '...')
        except Exception:
            pass
        result = await self._criadex.auth.create(
            api_key=api_key,
            create_config=AuthCreateConfig(
                master=False
            )
        )
        try:
            # Mask any api_key in the result when logging
            log_result = result.copy() if isinstance(result, dict) else result
            if isinstance(log_result, dict) and 'api_key' in log_result:
                log_result['api_key'] = str(log_result['api_key'])[:6] + '...'
            logger.debug("Bot auth creation result: %s", log_result)
        except Exception:
            pass
        # Optionally: check response for success or error
        return result

    async def _get_parent_ids_by_name(
        self,
        parent_names: List[str],
    ) -> Dict[str, int]:
        """
        Resolve parent bot names to IDs, raising if any are missing.
        """
        resolved: Dict[str, int] = {}

        for parent_name in parent_names:
            parent_id = await self._mysql_api.bots.retrieve_id(name=parent_name)
            if parent_id is None:
                raise ParentNotFoundError(
                    f"Parent bot '{parent_name}' does not exist."
                )
            resolved[parent_name] = parent_id

        return resolved

    async def _assign_parents(
        self,
        child_bot_id: int,
        parent_ids_by_name: Dict[str, int],
        parent_priorities: Dict[str, int],
    ) -> None:
        """
        Create BotParents relationships for a child bot.
        """
        from criabot.database.bots.tables.bot_parents import BotParentsConfig

        for parent_name, parent_id in parent_ids_by_name.items():
            priority = parent_priorities.get(parent_name, 0)
            await self._mysql_api.bot_parents.insert(
                BotParentsConfig(
                    child_bot_id=child_bot_id,
                    parent_bot_id=parent_id,
                    priority=priority,
                )
            )

    async def _authorize_child_on_parent_groups(
        self,
        child_api_key: str,
        parent_names: List[str],
    ) -> None:
        """
        Grant a child bot's API key read access on all parent groups.
        """
        from criabot.bot.bot import Bot

        for parent_name in parent_names:
            for index_type in ("DOCUMENT", "QUESTION"):
                group_name = Bot.bot_group_name(parent_name, index_type)
                try:
                    await self._criadex.group_auth.create(
                        group_name=group_name,
                        api_key=child_api_key,
                    )
                except Exception as e:
                    status_code = getattr(e, "status_code", None)
                    response = getattr(e, "response", None)
                    if status_code is None and response is not None:
                        status_code = getattr(response, "status_code", None)

                    message = str(e)
                    if status_code == 404 or "GROUP_NOT_FOUND" in message or "Group not found" in message:
                        logger.warning(
                            "Skipping authorization of child API key on missing parent group '%s': %s",
                            group_name,
                            message,
                        )
                        continue
                    raise

    async def get_parent_bot_names(self, name: str) -> List[str]:
        """
        Retrieve the list of direct parent bot names for a given bot.
        """
        bot_id = await self.get_id(name=name)
        parent_ids = await self._mysql_api.bot_parents.get_parent_ids(
            child_bot_id=bot_id
        )

        parent_names: List[str] = []
        for parent_id in parent_ids:
            parent_model = await self._mysql_api.bots.retrieve_by_id(parent_id)
            if parent_model is not None:
                parent_names.append(parent_model.name)

        return parent_names

    async def get_children_bot_names(self, name: str) -> List[str]:
        """
        Retrieve the list of direct child bot names for a given bot.
        """
        bot_id = await self.get_id(name=name)
        child_ids = await self._mysql_api.bot_parents.get_child_ids(
            parent_bot_id=bot_id
        )

        child_names: List[str] = []
        for child_id in child_ids:
            child_model = await self._mysql_api.bots.retrieve_by_id(child_id)
            if child_model is not None:
                child_names.append(child_model.name)

        return child_names

    async def _authorize_parents_on_child_groups(
        self,
        child_name: str,
        parent_ids_by_name: Dict[str, int],
    ) -> None:
        """
        Grant each parent's API key read access to the child's DOCUMENT/QUESTION groups.
        """
        from criabot.bot.bot import Bot

        for parent_name, parent_id in parent_ids_by_name.items():
            import inspect
            parent_key_candidate = self._mysql_api.bot_api_keys.get_active_by_bot(
                bot_id=parent_id
            )
            if inspect.isawaitable(parent_key_candidate):
                parent_key = await parent_key_candidate
            else:
                parent_key = parent_key_candidate

            if parent_key is None:
                continue

            for index_type in ("DOCUMENT", "QUESTION"):
                group_name = Bot.bot_group_name(child_name, index_type)
                await self._create_new_bot_auth_group(
                    group_name=group_name,
                    bot_api_key=parent_key.api_key,
                )

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
            try:
                await self._create_new_bot_auth_group(
                    group_name=group_name,
                    bot_api_key=bot_api_key
                )
            except Exception as e:
                import sys
                print(f"[ERROR] Failed to authorize bot API key on group {group_name}: {e}", file=sys.stderr)
                raise
            # Ensure the response has the group_name for consistency
            if isinstance(new_group, dict):
                if "group_name" not in new_group:
                    new_group["group_name"] = new_group.get("name", group_name)
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
        import httpx

        try:
            result = await self._criadex.manage.create(
                group_name=group_name,
                group_config=group_config
            )
            if isinstance(result, dict) and "group_name" not in result:
                result["group_name"] = result.get("name", group_name)
            if isinstance(result, dict):
                result["created"] = True
            return result
        except Exception as e:
            # Normalize HTTP status extraction for different exception types
            status_code = None
            if isinstance(e, httpx.HTTPStatusError):
                try:
                    status_code = e.response.status_code
                except Exception:
                    status_code = None
            else:
                status_code = getattr(e, 'status_code', None) or getattr(getattr(e, 'response', None), 'status_code', None)

            # If the group already exists, treat it as success and fetch its info
            if status_code == 409:
                try:
                    about = await self._criadex.manage.about(group_name=group_name)
                    if isinstance(about, dict) and 'group_name' not in about:
                        about['group_name'] = group_name
                    if isinstance(about, dict):
                        about["created"] = False
                    return about
                except Exception:
                    # Fall back to a minimal response indicating existence
                    return {"group_name": group_name, "created": False}

            # Re-raise for other errors
            raise

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
        try:
            logger.debug("Creating group auth for group='%s' (masked api_key)" , group_name)
        except Exception:
            pass
        max_attempts = 5
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                result = await self._criadex.group_auth.create(
                    group_name=group_name,
                    api_key=bot_api_key
                )
                try:
                    log_result = result.copy() if isinstance(result, dict) else result
                    if isinstance(log_result, dict) and 'api_key' in log_result:
                        log_result['api_key'] = str(log_result['api_key'])[:6] + '...'
                    logger.debug("Group auth creation succeeded: %s", log_result)
                except Exception:
                    pass
                return result
            except Exception as e:
                last_error = e
                message = str(e).lower()
                is_group_not_found = "group not found" in message or "group_not_found" in message

                if not is_group_not_found or attempt == max_attempts - 1:
                    logger.debug("Group auth creation FAILED: %s", e, exc_info=True)
                    raise

                logger.warning(
                    "Group '%s' not ready for auth creation yet; retrying (%d/%d).",
                    group_name,
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(1 + attempt)

        if last_error is not None:
            raise last_error

    @property
    def mysql_api(self) -> BotDatabaseAPI:
        return self._mysql_api

    @property
    def redis_api(self) -> "BotCacheAPI":
        return self._redis_api

    @property
    def criadex(self) -> RAGFlowSDK:
        return self._criadex
