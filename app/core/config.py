import os
from pathlib import Path

from dotenv import load_dotenv

from app.core.objects import AppMode, check_env_path

from criabot.schemas import CriadexCredentials, RedisCredentials, MySQLCredentials

ENV_PATH: str = os.environ.get('ENV_PATH', "./.env")

# Load .env configuration
ENV_LOADED: bool = load_dotenv(dotenv_path=check_env_path(ENV_PATH))

# FastAPI Config
APP_MODE: AppMode = AppMode[os.environ.get('APP_API_MODE', AppMode.TESTING.name)]
APP_HOST: str = "0.0.0.0"
APP_PORT: int = int(os.environ.get('APP_API_PORT', 25575))
APP_TITLE: str = "Criabot 🤖"
APP_VERSION = "1.0.0"
DOCS_URL: str = "/"


# Swagger Config
SWAGGER_TITLE: str = "Criabot API"
SWAGGER_FAVICON: str = "https://i.imgur.com/9XOI3qg.png"
SWAGGER_DESCRIPTION = f"""
<img width="40px" src="{SWAGGER_FAVICON}"/><br/><br/>
An asynchronous REST API built on [Ragflow](https://github.com/infiniflow/ragflow) for indexing/semantic search.
"""

# MySQL Config
MYSQL_CREDENTIALS: MySQLCredentials = MySQLCredentials(
    host=os.environ.get("MYSQL_HOST"),
    port=os.environ.get("MYSQL_PORT"),
    username=os.environ.get("MYSQL_USERNAME"),
    password=os.environ.get("MYSQL_PASSWORD"),
    database=os.environ.get("MYSQL_DATABASE")
)

# Criadex Config
CRIADEX_CREDENTIALS: CriadexCredentials = CriadexCredentials(
    api_base=os.environ.get("CRIADEX_API_BASE"),
    api_key=os.environ.get("CRIADEX_API_KEY"),
    master_api_key=os.environ.get("CRIADEX_MASTER_API_KEY")
)

# Redis Config
REDIS_CREDENTIALS: RedisCredentials = RedisCredentials(
    host=os.environ.get("REDIS_HOST"),
    port=os.environ.get("REDIS_PORT"),
    username=os.environ.get("REDIS_USERNAME"),
    password=os.environ.get("REDIS_PASSWORD"),
)

# By default, only enable in test mode, as stacktraces can leak sensitive info
CRIADEX_STACKTRACE: bool = os.environ.get("CRIADEX_STACKTRACE", APP_MODE == AppMode.TESTING)

# FAQ Sync Config
FAQ_SOURCE_URL: str = os.environ.get("FAQ_SOURCE_URL", "https://lthelp.yorku.ca/eclass")
FAQ_GROUP_NAME: str = os.environ.get("FAQ_GROUP_NAME", "eclass-faq-bot-document-index")
FAQ_SYNC_MAX_PAGES: int = int(os.environ.get("FAQ_SYNC_MAX_PAGES", "25"))
FAQ_SYNC_TIMEOUT_SECONDS: float = float(os.environ.get("FAQ_SYNC_TIMEOUT_SECONDS", "20"))

# Set the Tiktoken cache directory
os.environ["TIKTOKEN_CACHE_DIR"] = os.environ.get(
    "TIKTOKEN_CACHE_DIR",
    str(
        Path(os.environ.get("VIRTUAL_ENV", "./"))
        .joinpath("./tiktoken")
    )
)
