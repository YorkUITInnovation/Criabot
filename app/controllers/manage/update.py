from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.schemas import SUCCESS_CODE, \
    NOT_FOUND_CODE, BOT_MANAGEMENT_WRITE_RATE_LIMIT, exception_response, catch_exceptions, APIResponse, bot_management_limiter
from app.core.route import CriaRoute
from criabot.database.bots.tables.bot_params import BotParametersBaseConfig
from criabot.schemas import BotNotFoundError, BotUpdateConfig, CircularDependencyError, ParentNotFoundError

view = APIRouter()


class BotUpdateResponse(APIResponse):
    pass


@cbv(view)
class ManageUpdateRoute(CriaRoute):
    ResponseModel = BotUpdateResponse

    @view.patch(
        path="/bots/{bot_name}/manage/update",
        name="Configure Bot Hyperparameters",
        summary="Configure Bot Hyperparameters",
        description="Configure the hyperparameters for a Cria Bot",
    )
    @bot_management_limiter.limit(BOT_MANAGEMENT_WRITE_RATE_LIMIT)
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        BotNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That bot could not be found!"
        )
    )
    @exception_response(
        CircularDependencyError,
        ResponseModel(
            code="CIRCULAR_DEPENDENCY",
            status=400,
            message="Updating parent relationships would create a circular dependency."
        )
    )
    @exception_response(
        ParentNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="One or more specified parent bots do not exist."
        )
    )
    async def execute(
            self,
            request: Request,
            bot_name: str,
            config: BotUpdateConfig
    ) -> ResponseModel:
        # Input validation
        if not bot_name or not bot_name.strip():
            return self.ResponseModel(
                code="INVALID_INPUT",
                status=400,
                message="Bot name cannot be empty."
            )
        
        # Validate parent bot names if provided
        if config.parent_bot_names is not None:
            invalid_names = [name for name in config.parent_bot_names if not name or not name.strip()]
            if invalid_names:
                return self.ResponseModel(
                    code="INVALID_INPUT",
                    status=400,
                    message="Parent bot names cannot be empty."
                )
        
        # Update bot parameters if provided
        # Extract only the base parameters, excluding parent-related fields
        from criabot.database.bots.tables.bot_params import BotParametersBaseConfig
        base_params = BotParametersBaseConfig(**config.model_dump(exclude={"parent_bot_names", "parent_priorities"}))
        await request.app.criabot.update_parameters(
            name=bot_name,
            params=base_params
        )
        
        # Update parent relationships if provided
        if config.parent_bot_names is not None:
            await request.app.criabot.update_parent_relationships(
                child_name=bot_name,
                new_parent_names=config.parent_bot_names,
                parent_priorities=config.parent_priorities,
            )

        # Success!
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            message="Successfully updated the bot info."
        )


__all__ = ["view"]
