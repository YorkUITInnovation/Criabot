import json
import logging
import time
import traceback
from functools import wraps
from json import JSONDecodeError
from typing import Optional, Type, List, TypeVar, Callable, Awaitable

from CriadexSDK.core.api.route import CriadexError
from CriadexSDK.routers.content.search import Filter
from fastapi import Form
from pydantic import BaseModel, Field
from starlette import status
from starlette.exceptions import HTTPException

from criabot.bot.chat.chat import RelatedPrompt

SUCCESS_CODE: str = "SUCCESS"
RATE_LIMIT_CODE: str = "RATE_LIMIT"
UNAUTHORIZED_CODE: str = "UNAUTHORIZED"
ERROR_CODE: str = "ERROR"
DUPLICATE_CODE: str = "DUPLICATE"
NOT_FOUND_CODE: str = "NOT_FOUND"
CRIADEX_ERROR: str = "CRIADEX_ERROR"


class APIResponse(BaseModel):
    """
    Global API Response format that ALL responses must follow

    """

    status: int = 200
    message: Optional[str] = None
    timestamp: int = round(time.time())
    code: str = "SUCCESS"
    error: Optional[str] = Field(default=None, hidden=True)

    def dict(self, *args, **kwargs):

        self.message = self.message or {
            200: 'Request completed successfully!',
            409: 'The requested resource already exists!',
            400: 'You, the client, made a mistake...',
            500: 'An internal error occurred! :(',
            404: 'Womp womp. Not found!'
        }.get(self.status)

        data: dict = super().model_dump(*args, **kwargs)

        if data["error"] is None:
            del data["error"]

        return data

    class Config:
        @staticmethod
        def json_schema_extra(schema: dict, _):
            """Via https://github.com/tiangolo/fastapi/issues/1378"""
            props = {}
            for k, v in schema.get('properties', {}).items():
                if not v.get("hidden", False):
                    props[k] = v
            schema["properties"] = props


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
            except CriadexError as ex:

                log_message: str = (
                        traceback.format_exc() +
                        ex.response.model_dump_json(indent=4)
                )

                if ex.response.code == "ERROR":
                    logging.error(log_message)

                return output_shape(
                    code=ex.response.code,
                    status=ex.response.status,
                    message=f"[Criadex]: " + ex.response.message,
                    error=log_message
                )

            except Exception:
                logging.error(traceback.format_exc())
                return output_shape(
                    code="ERROR",
                    status=500,
                    message=f"An internal error occurred!",
                    error=traceback.format_exc()
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
