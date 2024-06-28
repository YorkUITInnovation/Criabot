import os
from enum import Enum


class AppMode(Enum):
    TESTING = 1
    PRODUCTION = 2


class EnvNotFoundException(FileNotFoundError):
    """Raised when the .env file cannot be found"""


def check_env_path(env_path: str) -> str:
    if not os.path.isfile(env_path):
        raise EnvNotFoundException(
            f"Failed to locate dotenv file at '{env_path}'. "
            f"Specify location with the ENV_PATH environment variable"
        )

    return env_path
