import json
import logging
import re
import time
import traceback
from functools import wraps
from json import JSONDecodeError
from typing import Optional, Type, List, TypeVar, Callable, Awaitable

import httpx
from CriadexSDK.ragflow_schemas import Filter
from fastapi import Form
from pydantic import BaseModel, Field, ConfigDict
from starlette import status
from starlette.exceptions import HTTPException

from criabot.bot.chat.schemas import RelatedPrompt
from slowapi import Limiter
from slowapi.util import get_remote_address

SUCCESS_CODE: str = "SUCCESS"
RATE_LIMIT_CODE: str = "RATE_LIMIT"
UNAUTHORIZED_CODE: str = "UNAUTHORIZED"
ERROR_CODE: str = "ERROR"
DUPLICATE_CODE: str = "DUPLICATE"
NOT_FOUND_CODE: str = "NOT_FOUND"
CRIADEX_ERROR: str = "CRIADEX_ERROR"

# Rate limiters - key by bot name for bot-specific endpoints, IP for general endpoints
bot_management_limiter: Limiter = Limiter(key_func=lambda request: request.path_params.get('bot_name', get_remote_address(request)))
chat_limiter: Limiter = Limiter(key_func=lambda request: request.path_params.get('chat_id', get_remote_address(request)))
general_limiter: Limiter = Limiter(key_func=get_remote_address)


def _sanitize_log_message(message: str) -> str:
    """
    Remove obvious API keys and authorization headers from log messages.
    This is a best-effort sanitizer to avoid leaking secrets into logs.
    """
    # X-Api-Key style headers
    message = re.sub(
        r"(X-Api-Key[\"']?\s*[:=]\s*[\"']?)([a-zA-Z0-9_\-]+)",
        r"\1[REDACTED]",
        message,
        flags=re.IGNORECASE,
    )

    # api_key query/body fields
    message = re.sub(
        r"(api_key[\"']?\s*[:=]\s*[\"']?)([a-zA-Z0-9_\-]+)",
        r"\1[REDACTED]",
        message,
        flags=re.IGNORECASE,
    )

    # Authorization: Bearer <token>
    message = re.sub(
        r"(Authorization[\"']?\s*[:=]\s*[\"']?Bearer\s+)([a-zA-Z0-9_\-]+)",
        r"\1[REDACTED]",
        message,
        flags=re.IGNORECASE,
    )

    return message


class APIResponse(BaseModel):
    """
    Global API Response format that ALL responses must follow

    """
    model_config = ConfigDict()

    status: int = 200
    message: Optional[str] = None
    timestamp: int = round(time.time())
    code: str = "SUCCESS"
    error: Optional[str] = Field(default=None)
    data: Optional[dict] = Field(default=None)

    def dict(self, *args, **kwargs):

        self.message = self.message or {
            200: 'Request completed successfully!',
            409: 'The requested resource already exists!',
            400: 'You, the client, made a mistake...',
            500: 'An internal error occurred! :(',
            404: 'Womp womp. Not found!'
        }.get(self.status)

        data: dict = super().model_dump(*args, exclude={'error'}, **kwargs)

        if data.get("error") is None:
            data.pop("error", None)

        return data


APIResponseModel = TypeVar('APIResponseModel', bound=APIResponse)


def catch_exceptions(
        output_shape: Type[APIResponse]
) -> Callable[..., Callable[..., Awaitable[APIResponseModel]]]:
    """
    Wrapper for controllers that handles exceptions & re-shapes them to match the response model

    :param output_shape:
    :return:
    """

    def error_handler(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> APIResponseModel:
            try:
                return await func(*args, **kwargs)
            except httpx.HTTPStatusError as ex:

                raw_log_message: str = traceback.format_exc() + ex.response.text
                safe_log_message = _sanitize_log_message(raw_log_message)

                logging.error(safe_log_message)

                return output_shape(
                    code=CRIADEX_ERROR,
                    status=ex.response.status_code,
                    message=f"[Criadex]: {ex.response.reason_phrase}",
                    error=safe_log_message
                )

            except Exception:
                logging.error(_sanitize_log_message(traceback.format_exc()))
                return output_shape(
                    code="ERROR",
                    status=500,
                    message=f"An internal error occurred!",
                    error=_sanitize_log_message(traceback.format_exc())
                )

        return wrapper

    return error_handler


def exception_response(
        exception: Type[Exception],
        response: APIResponse,
        log_error: bool = False
) -> Callable[..., Callable[..., Awaitable[APIResponseModel]]]:
    """
    Wrapper for controllers that handles exceptions & re-shapes them to match the response model

    :param exception: The exception to statically handle
    :param response: The response
    :param log_error
    :return:
    """

    def error_handler(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> APIResponseModel:
            try:
                return await func(*args, **kwargs)
            except exception:
                if log_error:
                    logging.getLogger("uvicorn.error").error(
                        "Caught Exception Response: " + traceback.format_exc()
                    )
                return response

        return wrapper

    return error_handler


class ChatSendConfig(BaseModel):
    prompt: str
    bot_name: str
    extra_bots: List[str] = Field(default_factory=list)

    metadata_filter: Optional[Filter] = {
        "must": [],
        "must_not": [],
        "should": [],
    }


class QuestionConfig(BaseModel):
    questions: List[str] = ["What is an index?", "What's this index thing?"]
    answer: str = "An index is an AI-powered database of information."
    llm_reply: bool = True
    related_prompts: List[RelatedPrompt] = Field(default_factory=list, examples=[[{"label": "Index Fundamentals", "prompt": "How does this differ from MySQL indexes?"}]])


class CriadexErrorResponse(APIResponse):
    criadex: dict


class RateLimitResponse(APIResponse):
    status: int = 429
    code: str = RATE_LIMIT_CODE


class UnauthorizedResponse(APIResponse):
    code: str = UNAUTHORIZED_CODE
    status: int = 401
    detail: Optional[str]


def form_metadata_converter(file_metadata: Optional[str] = Form(default=None)) -> Optional[dict]:
    try:
        return json.loads(file_metadata) if file_metadata else None
    except JSONDecodeError:
        raise HTTPException(
            detail="Invalid JSON string. Payload must be a JSON-serializable string.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
