import asyncio
import base64
import io
import os
import re
import secrets
import time
import zipfile
from datetime import datetime, timezone
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
from .database.faq.faq_db import FAQDatabaseAPI
from .database.gradebook.gradebook_db import GradebookDatabaseAPI
from .database.bots.tables.bot_params import BotParametersModel, BotParametersConfig, BotParametersBaseConfig
from .database.bots.tables.bots import BotsModel, BotsConfig
from .migrations.runner import MigrationRunner
from .faq.crawler import FAQCrawler
from .faq.indexer import FAQDocument, FAQIndexer
from .gradebook.analyzer import SyllabusAnalyzer
from .gradebook.conversation import ConversationManager
from .gradebook.schemas import CourseActivity, MoodleResource
from .gradebook.session import GradebookSessionEngine
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
        self._gradebook_analyzer: SyllabusAnalyzer = SyllabusAnalyzer(self._criadex)

        # Database
        self._mysql_engine = None
        self._mysql_api = None
        self._faq_api = None
        self._gradebook_api = None

        # Cache
        self._redis_pool = None
        self._redis_api = None

        # Gradebook runtime objects
        self._gradebook = None

        # FAQ website sync runtime config/state
        self._faq_sync_config: dict = {
            "source_url": os.environ.get("FAQ_SOURCE_URL", "https://lthelp.yorku.ca/eclass"),
            "group_name": os.environ.get("FAQ_GROUP_NAME", "eclass-faq-bot-document-index"),
            "max_pages": int(os.environ.get("FAQ_SYNC_MAX_PAGES", "25")),
            "timeout_seconds": float(os.environ.get("FAQ_SYNC_TIMEOUT_SECONDS", "20")),
            "enabled": os.environ.get("FAQ_SYNC_ENABLED", "false").lower() == "true",
            "interval_seconds": int(os.environ.get("FAQ_SYNC_INTERVAL_SECONDS", "21600")),
            "stale_after_seconds": int(os.environ.get("FAQ_STALE_AFTER_SECONDS", "43200")),
            "failure_alert_threshold": int(os.environ.get("FAQ_ALERT_FAILURE_THRESHOLD", "3")),
        }
        self._gradebook_syllabus_group_name: Optional[str] = os.environ.get("GRADEBOOK_SYLLABUS_GROUP_NAME")
        self._faq_sync_status: dict = {
            "last_run_at": None,
            "last_success_at": None,
            "state": "NOT_RUN",
            "pages_crawled": 0,
            "indexed_files": 0,
            "duplicate_files": 0,
            "error": None,
            "consecutive_failures": 0,
            "alert_state": "IDLE",
            "scheduler_running": False,
            "last_graph_build_job": None,
            "recent_runs": [],
        }
        self._faq_sync_lock = asyncio.Lock()
        self._faq_sync_task: Optional[asyncio.Task] = None

        self._already_initialized = False
        self._gradebook = GradebookSessionEngine(
            criadex=self._criadex,
            mapping_llm_model_id=os.environ.get("GRADEBOOK_MAPPING_LLM_MODEL_ID", "gpt-3.5-turbo"),
        )
        self._gradebook_conversation = ConversationManager()

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
        await MigrationRunner(self._mysql_engine).run_pending()

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

        # Gradebook DB API Startup
        self._gradebook_api: GradebookDatabaseAPI = GradebookDatabaseAPI(engine=self._mysql_engine)
        await self._gradebook_api.initialize()

        # FAQ DB API Startup
        self._faq_api: FAQDatabaseAPI = FAQDatabaseAPI(engine=self._mysql_engine)
        await self._faq_api.initialize()
        await self._refresh_faq_sync_status_from_db()

        # Redis API Startup
        from .cache.api import BotCacheAPI
        self._redis_api = BotCacheAPI(pool=self._redis_pool)

        # Initialize gradebook engine with database + cache
        self._gradebook = GradebookSessionEngine(
            gradebook_db=self._gradebook_api,
            gradebook_cache=self._redis_api.gradebooks,
            criadex=self._criadex,
            mapping_llm_model_id=os.environ.get("GRADEBOOK_MAPPING_LLM_MODEL_ID", "gpt-3.5-turbo"),
        )

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
                faq_fallback_enabled=params_model.faq_fallback_enabled,
                faq_fallback_threshold=params_model.faq_fallback_threshold,
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
                    await self._create_new_bot_auth_group(
                        group_name=group_name,
                        bot_api_key=child_api_key,
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
                    "rerank_model_id": bot_config.rerank_model_id,
                    "use_knowledge_graph": bot_config.use_knowledge_graph,
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
    def gradebook_api(self) -> GradebookDatabaseAPI:
        return self._gradebook_api

    @property
    def faq_api(self) -> FAQDatabaseAPI:
        return self._faq_api

    @property
    def redis_api(self) -> "BotCacheAPI":
        return self._redis_api

    @property
    def criadex(self) -> RAGFlowSDK:
        return self._criadex

    async def analyze_syllabus_group(
        self,
        group_name: str,
        prompt: str,
        top_k: int = 8,
        max_hops: int = 1,
        max_expansion_terms: int = 8,
    ) -> dict:
        analyzer = SyllabusAnalyzer(criadex=self._criadex)
        return await analyzer.analyze_group(
            group_name=group_name,
            prompt=prompt,
            top_k=top_k,
            max_hops=max_hops,
            max_expansion_terms=max_expansion_terms,
        )

    async def sync_faq_group(
        self,
        group_name: str,
        documents: List[FAQDocument],
        trigger_graph_build: bool = True,
    ) -> dict:
        indexer = FAQIndexer(criadex=self._criadex)
        return await indexer.sync_group(
            group_name=group_name,
            documents=documents,
            trigger_graph_build=trigger_graph_build,
        )

    async def sync_faq_site(
        self,
        source_url: Optional[str] = None,
        group_name: Optional[str] = None,
        max_pages: Optional[int] = None,
        trigger_graph_build: bool = True,
    ) -> dict:
        effective_source = source_url or self._faq_sync_config["source_url"]
        effective_group = group_name or self._faq_sync_config["group_name"]
        effective_max_pages = int(max_pages or self._faq_sync_config["max_pages"])
        timeout_seconds = float(self._faq_sync_config["timeout_seconds"])
        run_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        run_started_ts = int(time.time())

        async with self._faq_sync_lock:
            self._faq_sync_status.update(
                {
                    "last_run_at": run_started_ts,
                    "state": "RUNNING",
                    "error": None,
                }
            )

            try:
                crawler = FAQCrawler(timeout_seconds=timeout_seconds)
                pages = await crawler.crawl(source_url=effective_source, max_pages=effective_max_pages)
                documents: List[FAQDocument] = [
                    FAQDocument(
                        file_name=f"faq-page-{idx + 1}",
                        file_contents={
                            "nodes": [
                                {
                                    "text": page["text"],
                                    "type": "UncategorizedText",
                                    "metadata": {},
                                }
                            ]
                        },
                        file_metadata={
                            "source": "eclass_website",
                            "url": page["url"],
                            "title": page.get("title"),
                        },
                    )
                    for idx, page in enumerate(pages)
                ]

                sync_result = await self.sync_faq_group(
                    group_name=effective_group,
                    documents=documents,
                    trigger_graph_build=trigger_graph_build,
                )
                duplicate_files = len(sync_result.get("duplicate_files", []))
                graph_build_job = sync_result.get("graph_build_job")
                self._faq_sync_status.update(
                    {
                        "state": "READY",
                        "pages_crawled": len(pages),
                        "indexed_files": len(sync_result.get("uploaded_files", [])),
                        "duplicate_files": duplicate_files,
                        "last_success_at": int(time.time()),
                        "error": None,
                        "last_graph_build_job": graph_build_job,
                    }
                )
                await self._record_faq_sync_log(
                    run_at=run_started_at,
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    source_url=effective_source,
                    group_name=effective_group,
                    max_pages=effective_max_pages,
                    timeout_seconds=timeout_seconds,
                    trigger_graph_build=trigger_graph_build,
                    state="READY",
                    pages_crawled=len(pages),
                    indexed_files=len(sync_result.get("uploaded_files", [])),
                    duplicate_files=duplicate_files,
                    graph_build_job=graph_build_job,
                )
                await self._refresh_faq_sync_status_from_db()
                return {
                    **sync_result,
                    "source_url": effective_source,
                    "pages_crawled": len(pages),
                }
            except Exception as ex:
                self._faq_sync_status.update(
                    {
                        "state": "ERROR",
                        "error": str(ex),
                        "duplicate_files": 0,
                        "last_graph_build_job": None,
                    }
                )
                await self._record_faq_sync_log(
                    run_at=run_started_at,
                    completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    source_url=effective_source,
                    group_name=effective_group,
                    max_pages=effective_max_pages,
                    timeout_seconds=timeout_seconds,
                    trigger_graph_build=trigger_graph_build,
                    state="ERROR",
                    error=str(ex),
                )
                await self._refresh_faq_sync_status_from_db()
                raise

    def get_faq_sync_status(self) -> dict:
        return {
            **self._faq_sync_status,
            **self._build_faq_health_status(),
            "config": self._faq_sync_config.copy(),
        }

    def update_faq_sync_config(
        self,
        source_url: Optional[str] = None,
        group_name: Optional[str] = None,
        max_pages: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
        interval_seconds: Optional[int] = None,
        stale_after_seconds: Optional[int] = None,
        failure_alert_threshold: Optional[int] = None,
    ) -> dict:
        if source_url is not None:
            self._faq_sync_config["source_url"] = source_url
        if group_name is not None:
            self._faq_sync_config["group_name"] = group_name
        if max_pages is not None:
            self._faq_sync_config["max_pages"] = int(max_pages)
        if timeout_seconds is not None:
            self._faq_sync_config["timeout_seconds"] = float(timeout_seconds)
        if enabled is not None:
            self._faq_sync_config["enabled"] = bool(enabled)
        if interval_seconds is not None:
            self._faq_sync_config["interval_seconds"] = int(interval_seconds)
        if stale_after_seconds is not None:
            self._faq_sync_config["stale_after_seconds"] = int(stale_after_seconds)
        if failure_alert_threshold is not None:
            self._faq_sync_config["failure_alert_threshold"] = int(failure_alert_threshold)
        return self._faq_sync_config.copy()

    async def _record_faq_sync_log(
        self,
        run_at: datetime,
        completed_at: Optional[datetime],
        source_url: str,
        group_name: str,
        max_pages: int,
        timeout_seconds: float,
        trigger_graph_build: bool,
        state: str,
        pages_crawled: int = 0,
        indexed_files: int = 0,
        duplicate_files: int = 0,
        error: Optional[str] = None,
        graph_build_job: Optional[dict] = None,
    ) -> None:
        if self._faq_api is None:
            return

        from criabot.database.faq.tables.faq_sync_logs import FAQSyncLogConfig

        await self._faq_api.sync_logs.insert(
            FAQSyncLogConfig(
                run_at=run_at,
                completed_at=completed_at,
                source_url=source_url,
                group_name=group_name,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
                trigger_graph_build=trigger_graph_build,
                state=state,
                pages_crawled=pages_crawled,
                indexed_files=indexed_files,
                duplicate_files=duplicate_files,
                error=error,
                graph_build_job=graph_build_job,
            )
        )

    async def _refresh_faq_sync_status_from_db(self) -> None:
        if self._faq_api is None:
            return

        recent_runs = await self._faq_api.sync_logs.retrieve_latest(limit=5)
        recent_run_data = [
            {
                "run_at": int(run.run_at.timestamp()),
                "completed_at": int(run.completed_at.timestamp()) if run.completed_at else None,
                "state": run.state,
                "pages_crawled": run.pages_crawled,
                "indexed_files": run.indexed_files,
                "duplicate_files": run.duplicate_files,
                "error": run.error,
            }
            for run in recent_runs
        ]
        consecutive_failures = 0
        for run in recent_runs:
            if run.state == "ERROR":
                consecutive_failures += 1
                continue
            break

        latest_run = recent_runs[0] if recent_runs else None
        latest_success = next((run for run in recent_runs if run.state == "READY"), None)
        if latest_run is not None:
            self._faq_sync_status.update(
                {
                    "last_run_at": int(latest_run.run_at.timestamp()),
                    "state": latest_run.state,
                    "pages_crawled": latest_run.pages_crawled,
                    "indexed_files": latest_run.indexed_files,
                    "duplicate_files": latest_run.duplicate_files,
                    "error": latest_run.error,
                    "last_graph_build_job": latest_run.graph_build_job,
                }
            )
        if latest_success is not None:
            self._faq_sync_status["last_success_at"] = int(latest_success.completed_at.timestamp()) if latest_success.completed_at else int(latest_success.run_at.timestamp())

        self._faq_sync_status["consecutive_failures"] = consecutive_failures
        self._faq_sync_status["recent_runs"] = recent_run_data
        self._faq_sync_status["alert_state"] = self._build_faq_health_status()["alert_state"]

    def _build_faq_health_status(self) -> dict:
        now = int(time.time())
        last_success_at = self._faq_sync_status.get("last_success_at")
        stale_after_seconds = int(self._faq_sync_config.get("stale_after_seconds", 43200))
        failure_alert_threshold = int(self._faq_sync_config.get("failure_alert_threshold", 3))
        stale = bool(last_success_at and now - int(last_success_at) > stale_after_seconds)
        consecutive_failures = int(self._faq_sync_status.get("consecutive_failures", 0) or 0)

        if consecutive_failures >= failure_alert_threshold:
            alert_state = "FAILURE_THRESHOLD_EXCEEDED"
        elif stale:
            alert_state = "STALE_DATA"
        elif self._faq_sync_status.get("state") == "RUNNING":
            alert_state = "SYNC_RUNNING"
        elif last_success_at:
            alert_state = "HEALTHY"
        else:
            alert_state = "IDLE"

        if alert_state == "FAILURE_THRESHOLD_EXCEEDED":
            logger.error(
                "FAQ sync failure threshold exceeded (%s consecutive failures).",
                consecutive_failures,
            )
        elif alert_state == "STALE_DATA":
            logger.warning("FAQ data is stale; last successful sync was at %s.", last_success_at)

        return {
            "stale": stale,
            "consecutive_failures": consecutive_failures,
            "alert_state": alert_state,
        }

    async def start_faq_sync_scheduler(self) -> None:
        if self._faq_sync_task is not None and not self._faq_sync_task.done():
            return
        if not self._faq_sync_config.get("enabled", False):
            return

        self._faq_sync_status["scheduler_running"] = True
        self._faq_sync_task = asyncio.create_task(self._faq_sync_scheduler_loop())

    async def stop_faq_sync_scheduler(self) -> None:
        self._faq_sync_status["scheduler_running"] = False
        if self._faq_sync_task is None:
            return
        self._faq_sync_task.cancel()
        try:
            await self._faq_sync_task
        except asyncio.CancelledError:
            pass
        self._faq_sync_task = None

    async def _faq_sync_scheduler_loop(self) -> None:
        interval_seconds = max(int(self._faq_sync_config.get("interval_seconds", 21600)), 60)
        while True:
            try:
                status = self.get_faq_sync_status()
                should_run = status["last_success_at"] is None or status["stale"]
                if should_run and status["state"] != "RUNNING":
                    await self.sync_faq_site(trigger_graph_build=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled FAQ sync failed.")

            await asyncio.sleep(interval_seconds)

    async def start_gradebook_session(
        self,
        course_id: str,
        professor_id: str,
        bot_name: str,
        moodle_resources: List[dict],
        course_activities: List[dict],
    ) -> dict:
        resources = [MoodleResource(**resource) for resource in moodle_resources]
        activities = [CourseActivity(**activity) for activity in course_activities]
        session = await self._gradebook.start(
            course_id=course_id,
            professor_id=professor_id,
            bot_name=bot_name,
            moodle_resources=resources,
            course_activities=activities,
        )

        initial_message = (
            "I've found syllabus-like content and started analysis."
            if session.phase == "ANALYSIS"
            else "I couldn't find a syllabus yet. Please provide syllabus details to continue."
        )

        if session.phase == "ANALYSIS" and self._gradebook_syllabus_group_name:
            try:
                extraction = await self._gradebook_analyzer.extract_assessment_structure(
                    group_name=self._gradebook_syllabus_group_name
                )
                base_extraction = session.extraction or {}
                if not isinstance(base_extraction, dict):
                    base_extraction = {}
                base_extraction["analysis"] = extraction
                session.extraction = base_extraction
                await self._gradebook_api.sessions.update_session(
                    session_id=session.session_id,
                    updates={"extraction_json": session.extraction}
                )
            except Exception:
                logger.warning("Gradebook syllabus extraction failed; continuing without structured analysis.")

        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "initial_message": initial_message,
        }

    async def gradebook_status(self, session_id: str) -> dict:
        session = await self._gradebook.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")
        return session.model_dump()

    async def gradebook_chat(self, session_id: str, prompt: str) -> dict:
        session = await self._gradebook.chat(session_id=session_id, prompt=prompt)
        reply_message = self._gradebook_conversation.make_reply(session=session, proposal=session.proposal, prompt=prompt)
        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "reply": reply_message,
            "proposal": session.proposal.model_dump() if session.proposal else None,
        }

    @staticmethod
    def _extract_text_from_docx(file_bytes: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                chunks = []
                for name in ("word/document.xml", "word/header1.xml", "word/footer1.xml"):
                    if name in zf.namelist():
                        chunks.append(zf.read(name).decode("utf-8", errors="ignore"))
                if not chunks:
                    return ""
                xml_text = "\n".join(chunks)
                text = re.sub(r"<[^>]+>", " ", xml_text)
                text = re.sub(r"\s+", " ", text).strip()
                return text
        except Exception:
            return ""

    @staticmethod
    def _extract_text_from_pdf(file_bytes: bytes) -> str:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(file_bytes))
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            text = "\n".join(parts)
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            try:
                # Fallback for environments where pypdf is unavailable:
                # decode whatever text-like bytes are present to avoid hard upload failure.
                fallback = file_bytes.decode("latin-1", errors="ignore")
                fallback = re.sub(r"\s+", " ", fallback).strip()
                return fallback[:4000]
            except Exception:
                return ""

    async def gradebook_upload(self, session_id: str, filename: str, filetype: str, base64_content: str) -> dict:
        file_bytes = base64.b64decode(base64_content or "")
        ext = os.path.splitext(filename or "")[1].lower()
        mimetype = (filetype or "").lower()

        extracted_text = ""
        if ext in {".txt", ".md"} or mimetype.startswith("text/"):
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
        elif ext == ".docx" or "wordprocessingml" in mimetype:
            extracted_text = self._extract_text_from_docx(file_bytes)
        elif ext == ".pdf" or mimetype == "application/pdf":
            extracted_text = self._extract_text_from_pdf(file_bytes)
        else:
            extracted_text = file_bytes.decode("utf-8", errors="ignore")

        extracted_text = (extracted_text or "").strip()
        if not extracted_text:
            raise ValueError("Could not extract text from uploaded document.")

        filename_l = (filename or "").lower()
        looks_like_syllabus = any(token in filename_l for token in (
            "syllabus", "syllabi", "syllabe", "plan de cours", "plan_du_cours", "outline", "programme"
        ))
        text_l = extracted_text.lower()
        if not looks_like_syllabus:
            looks_like_syllabus = (
                "grading" in text_l
                or "assessment" in text_l
                or "barème" in text_l
                or "plan de cours" in text_l
                or "%" in extracted_text
            )

        clipped = extracted_text[:8000]
        if looks_like_syllabus:
            ingest_prompt = (
                f"I uploaded a syllabus document named '{filename}'. "
                f"Please analyze this grading information and update the gradebook proposal accordingly:\n\n{clipped}"
            )
        else:
            ingest_prompt = (
                f"I uploaded a supporting course document named '{filename}'. "
                f"Use this context to improve the gradebook proposal and mapping decisions:\n\n{clipped}"
            )

        session = await self._gradebook.chat(session_id=session_id, prompt=ingest_prompt)

        extraction = session.extraction or {}
        if not isinstance(extraction, dict):
            extraction = {}
        if looks_like_syllabus:
            extraction["has_syllabus"] = True
        sources = list(extraction.get("syllabus_sources") or [])
        if looks_like_syllabus and filename and filename not in sources:
            sources.append(filename)
        extraction["syllabus_sources"] = sources
        if looks_like_syllabus:
            extraction["uploaded_syllabus_text"] = clipped
        supporting = list(extraction.get("supporting_documents") or [])
        if filename and filename not in supporting:
            supporting.append(filename)
        extraction["supporting_documents"] = supporting
        session.extraction = extraction
        await self._gradebook_api.sessions.update_session(
            session_id=session.session_id,
            updates={"extraction_json": extraction},
        )

        reply_prompt = "uploaded syllabus document" if looks_like_syllabus else "uploaded supporting document"
        reply_message = self._gradebook_conversation.make_reply(
            session=session,
            proposal=session.proposal,
            prompt=reply_prompt,
        )
        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "reply": reply_message,
            "proposal": session.proposal.model_dump() if session.proposal else None,
        }

    async def gradebook_proposal(self, session_id: str) -> dict:
        session = await self._gradebook.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")
        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "proposal": session.proposal.model_dump() if session.proposal else None,
        }

    async def gradebook_accept(self, session_id: str) -> dict:
        session = await self._gradebook.accept(session_id=session_id)
        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "proposal": session.proposal.model_dump() if session.proposal else None,
            "content_mapping": session.content_mapping,
        }

    async def gradebook_finalize(
        self,
        session_id: str,
        confirmed_mapping: List[dict],
        create_categories: bool = True,
        reorganize_resources: bool = False,
    ) -> dict:
        session = await self._gradebook.finalize(
            session_id=session_id,
            confirmed_mapping=confirmed_mapping,
        )
        proposal = session.proposal.model_dump() if session.proposal else None
        total_weight = 0
        if proposal and isinstance(proposal.get("categories"), list):
            for category in proposal["categories"]:
                try:
                    total_weight += float(category.get("weight") or 0)
                except (TypeError, ValueError):
                    continue
        graded_count = sum(1 for item in confirmed_mapping if item.get("category"))
        not_graded_count = len(confirmed_mapping) - graded_count
        summary = {
            "categories_to_create": len({item.get("category") for item in confirmed_mapping if item.get("category")}),
            "activities_mapped": len(confirmed_mapping),
            "graded_count": graded_count,
            "not_graded_count": not_graded_count,
            "total_weight": round(total_weight, 2) if total_weight else None,
            "create_categories": create_categories,
            "reorganize_resources": reorganize_resources,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
        }
        return {
            "session_id": session.session_id,
            "phase": session.phase,
            "summary": summary,
            "content_mapping": session.content_mapping,
            "proposal": proposal,
        }
