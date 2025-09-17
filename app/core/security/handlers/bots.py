from json import JSONDecodeError
from typing import Optional, Type, Awaitable

from CriadexSDK.routers.auth import AuthCheckRoute
from CriadexSDK.routers.group_auth import GroupAuthCheckRoute
from starlette.requests import Request

from app.controllers.schemas import APIResponse
from app.core.security.get_api_key import GetApiKey, BadAPIKeyException

BotNameFuncType: Type = Awaitable[str]


def default_bot_name_fn(request: Request) -> str:
    return request.path_params.get('bot_name')


class GetApiKeyBots(GetApiKey):

    async def read_bot_name(self) -> Optional[str]:

        # Grab from the params
        bot_name: Optional[str] = (
                self.request.path_params.get("bot_name")
                or self.request.query_params.get("bot_name")
        )

        if bot_name is not None:
            return bot_name

        # Grab from the JSON payload
        try:
            json_body: dict = await self.request.json()
            bot_name = (json_body or dict()).get("bot_name")
        except JSONDecodeError:
            pass

        return bot_name

    async def execute(self) -> str:
        auth_response: AuthCheckRoute.Response = await self.get_auth()

        # Master keys go brr
        if auth_response.master:
            return self.api_key

        # Since they are NOT master but trying to access stack trace, throw an error
        # It's a security concern to give stacktraces as it leaks implementation
        if APIResponse.stack_trace_enabled(self.request):
            raise BadAPIKeyException(
                status_code=401,
                detail="Only master keys can access stacktraces!"
            )

        bot_name: Optional[str] = await self.read_bot_name()

        if not bot_name:
            raise BadAPIKeyException(
                status_code=400,
                detail="Bot name not included in params!"
            )

        from criabot.bot.bot import Bot
        test_group_name: str = Bot.bot_group_name(bot_name, "DOCUMENT")
        group_response: GroupAuthCheckRoute.Response = await self.get_group_auth(test_group_name)

        if not group_response.authorized:
            raise BadAPIKeyException(
                status_code=401,
                detail="Your key is not authorized for accessing this bot."
            )

        return self.api_key
