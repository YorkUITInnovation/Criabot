from typing import Optional

from CriadexSDK.ragflow_schemas import AuthCreateResponse
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import DUPLICATE_CODE, SUCCESS_CODE, exception_response, catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute
from criabot.schemas import BotCreateConfig, BotExistsError, ParentNotFoundError, CircularDependencyError, InvalidModelsError

view = APIRouter()


class BotCreateResponse(APIResponse):
    bot_api_key: Optional[str] = None


@cbv(view)
class ManageCreateRoute(CriaRoute):
    ResponseModel = BotCreateResponse

    @view.post(
        path="/bots/{bot_name}/manage/create",
        name="Create a Cria Bot",
        summary="Create a Cria Bot",
        description="Create a Cria Bot.",
    )
    @bot_management_limiter.limit("10/minute")
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        BotExistsError,
        ResponseModel(
            code=DUPLICATE_CODE,
            status=409,
            message="That bot already exists!"
        )
    )
    @exception_response(
        ParentNotFoundError,
        ResponseModel(
            code="PARENT_NOT_FOUND",
            status=404,
            message="One or more specified parent bots do not exist."
        )
    )
    @exception_response(
        CircularDependencyError,
        ResponseModel(
            code="INVALID_PARENT_RELATIONSHIP",
            status=400,
            message="The specified parents would create a circular dependency."
        )
    )
    @exception_response(
        InvalidModelsError,
        ResponseModel(
            code="INVALID_MODEL",
            status=400,
            message="One or more specified model IDs are invalid."
        )
    )
    async def execute(
            self,
            request: Request,
            bot_name: str,
            config: BotCreateConfig
    ) -> ResponseModel:
        # Input validation
        if not bot_name or not bot_name.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Bot name cannot be empty."
            )
        
        if len(bot_name) > 128:
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Bot name exceeds maximum length of 128 characters."
            )
        
        # Validate parent bot names if provided
        if config.parent_bot_names:
            invalid_names = [name for name in config.parent_bot_names if not name or not name.strip()]
            if invalid_names:
                return self.ResponseModel(
                    code="INVALID_INPUT",
                    status=400,
                    message="Parent bot names cannot be empty."
                )
        
        # Try to create the bot
        auth_response: AuthCreateResponse = await request.app.criabot.create(
            name=bot_name,
            config=config
        )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully created the bot & their indexes.",
            bot_api_key=auth_response['api_key']
        )


__all__ = ["view"]