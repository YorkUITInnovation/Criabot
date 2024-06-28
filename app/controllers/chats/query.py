from CriadexSDK.routers.content.search import CompletionUsage
from fastapi import APIRouter
from fastapi_restful.cbv import cbv
from starlette.requests import Request

from app.controllers.chats.send import BotChatSendResponse
from app.controllers.schemas import SUCCESS_CODE, NOT_FOUND_CODE, ChatSendConfig, \
    exception_response, catch_exceptions
from app.core.route import CriaRoute
from criabot.bot.bot import Bot
from criabot.bot.chat.chat import ChatReply, Chat
from criabot.bot.schemas import ChatNotFoundError

view = APIRouter()


class BotChatQueryResponse(BotChatSendResponse):
    pass


@cbv(view)
class QueryChatRoute(CriaRoute):
    ResponseModel = BotChatQueryResponse

    @view.post(
        path="/bots/chats/query",
        name="Query a Bot",
        summary="Query a bot in a 1-message chat",
        description="Query a bot in a 1-message chat",
        # dependencies=CHATS_BOT_DEPS
    )
    @catch_exceptions(
        ResponseModel
    )
    @exception_response(
        ChatNotFoundError,
        ResponseModel(
            code=NOT_FOUND_CODE,
            status=404,
            message="That chat does not exist or is expired...which doesn't make sense."
        )
    )
    async def execute(
            self,
            request: Request,
            chat_config: ChatSendConfig
    ) -> ResponseModel:

        # Check the bots exist
        if not await request.app.criabot.exists(*[chat_config.bot_name, *chat_config.extra_bots]):
            return self.ResponseModel(
                code=NOT_FOUND_CODE,
                status=404,
                message="One or more bots could not be found in the query."
            )

        # Try to start a chat
        chat_id: str = await Bot.start_chat(cache_api=request.app.criabot.redis_api)

        # Get the chat
        chat: Chat = await request.app.criabot.get_bot_chat(chat_id=chat_id, bot_name=chat_config.bot_name)

        # Then send the prompt
        reply: ChatReply = await chat.send(
            prompt=chat_config.prompt,
            metadata_filter=chat_config.metadata_filter,
            extra_bots=chat_config.extra_bots
        )

        # End the chat
        await request.app.criabot.end_bot_chat(chat_id)

        # Return the response
        return self.ResponseModel(
            code=SUCCESS_CODE,
            status=200,
            messsage="Successfully send the chat",
            reply=reply,
        )


__all__ = ["view"]
