from __future__ import annotations

import logging
import os
import traceback
import warnings
from contextlib import asynccontextmanager
from typing import Any, List, AsyncContextManager

from redis import asyncio as aioredis
from fastapi import FastAPI
from starlette.datastructures import State
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

from app.controllers import router
from app.core.security.get_api_key import GetApiKey, BadAPIKeyException
from criabot.criabot import Criabot
from . import config
from .middleware import StatusMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse
from app.controllers.schemas import RateLimitResponse


class CriabotAPI(FastAPI):
    """
    FastAPI
    """

    ORIGINS: List[str] = [os.environ.get("APP_API_ORIGINS", "*")]

    def __init__(
            self,
            **extra: Any
    ):
        super().__init__(**extra)

        # FastAPI Setup
        self.state: State = getattr(self, 'state', None)
        self.logger: logging.Logger = logging.getLogger('uvicorn.info')

        # Criadex Setup
        self.criabot: Optional[Criabot] = None

    @classmethod
    def create(cls) -> CriabotAPI:
        """
        Generate an instance of the app

        :return: Instance of the FastAPI app

        """

        # Make more stuff
        # Enable API docs in non-production mode
        from app.core.objects import AppMode
        docs_url = "/docs" if config.APP_MODE != AppMode.PRODUCTION else None
        openapi_url = "/openapi.json" if config.APP_MODE != AppMode.PRODUCTION else None
        
        _app: CriabotAPI = CriabotAPI(
            title=config.APP_TITLE,
            description=config.SWAGGER_DESCRIPTION,
            docs_url=docs_url,
            openapi_url=openapi_url,
            version=config.APP_VERSION,
            lifespan=cls.app_lifespan
        )

        # Add extra bells & whistles
        _app.include_router(router)
        _app.add_exception_handler(BadAPIKeyException, GetApiKey.handle_no_auth)
        _app.add_exception_handler(RateLimitExceeded, _app.rate_limit_handler)
        
        # Initialize rate limiters on app state
        from app.controllers.schemas import bot_management_limiter, chat_limiter, general_limiter
        _app.state.bot_management_limiter = bot_management_limiter
        _app.state.chat_limiter = chat_limiter
        _app.state.general_limiter = general_limiter
        
        _app.include_middlewares()

        # Please shut up
        logging.getLogger('asyncio').setLevel(logging.CRITICAL)
        warnings.filterwarnings('ignore', module='aiomysql')

        return _app

    def include_middlewares(self) -> None:
        """
        Include CORS handling

        :return: None

        """

        self.add_middleware(
            CORSMiddleware,
            allow_origins=self.ORIGINS,
            allow_credentials=True,
            allow_methods=self.ORIGINS,
            allow_headers=self.ORIGINS,
        )

        self.add_middleware(
            StatusMiddleware
        )

    @classmethod
    def rate_limit_handler(cls, request: Request, _: RateLimitExceeded) -> JSONResponse:
        """
        Handle rate limit exceeded errors
        
        :param request: The request
        :param _: The rate limit exception
        :return: JSON response with rate limit error
        """
        response = RateLimitResponse(
            message="Rate limit exceeded. Please try again later.",
            code="RATE_LIMIT",
            status=429
        )
        return JSONResponse(
            content=response.dict(),
            status_code=429
        )

    async def preflight_checks(self) -> bool:
        """
        Run preflight checks to confirm app is ready to "fly"

        :return: Whether to kill the app startup

        """

        preflight_failed: bool = False

        # Check if in docker
        if os.environ.get('IN_DOCKER'):
            self.logger.info("Application loaded within a Docker container.")

        # Check if .env files loaded
        if config.ENV_LOADED:
            self.logger.info("Loaded '.env' configuration file with environment variables.")

        # Check if successful
        if preflight_failed:
            self.logger.error('Application failed preflight checks and will not be able to run.')
            return False

        return True

    async def postflight_checks(self) -> bool:

        # Redis doesn't ping until you execute a command, so let's check it's on
        async with aioredis.Redis(connection_pool=self.criabot.redis_api.pool) as redis:
            try:
                await redis.ping()
            except ConnectionRefusedError:
                logging.error("Failed to ping Redis: " + traceback.format_exc())
                return False

        return True

    @staticmethod
    @asynccontextmanager
    async def app_lifespan(criabot_api: CriabotAPI) -> AsyncContextManager[None]:
        """
        Handle the lifespan of the app

        :return: Context manager for Criadex

        """

        # Preflight Checks
        if not await criabot_api.preflight_checks():
            exit()

        # Initialization
        # Create and initialize Criabot here
        criabot_instance = Criabot(
            criadex_credentials=config.CRIADEX_CREDENTIALS,
            mysql_credentials=config.MYSQL_CREDENTIALS,
            redis_credentials=config.REDIS_CREDENTIALS,
            criadex_stacktrace=config.CRIADEX_STACKTRACE
        )
        await criabot_instance.initialize()
        criabot_api.criabot = criabot_instance # Assign to app instance
        await criabot_instance.start_faq_sync_scheduler()

        # Postflight checks
        if not await criabot_api.postflight_checks():
            exit()

        # Shutdown is after yield
        yield

        criabot_api.logger.info("Shutting down Criabot...")
        await criabot_instance.stop_faq_sync_scheduler()
        # Optionally, add shutdown logic for criabot_instance here


# Instance of the app, started by Uvicorn.
app: CriabotAPI = CriabotAPI.create()
