import json
import os
import re
from typing import List, Optional, Any

from redis import asyncio as aioredis
from CriadexSDK.ragflow_schemas import ChatMessage, TextBlock
from pydantic import BaseModel

from criabot.bot.chat.buffer import ChatBuffer
from criabot.cache.core import CacheObject


def _parse_time_to_seconds(time_str: str) -> int:
    if not time_str:
        return 3600
    match = re.match(r'^(\d+)([hdwmy])$', time_str.lower())
    if not match:
        return 3600
    value, unit = match.groups()
    value = int(value)
    if unit == 'h':
        return value * 60 * 60
    elif unit == 'd':
        return value * 24 * 60 * 60
    elif unit == 'w':
        return value * 7 * 24 * 60 * 60
    elif unit == 'm':
        return value * 30 * 24 * 60 * 60
    elif unit == 'y':
        return value * 365 * 24 * 60 * 60
    return 3600


CHAT_EXPIRE_TIME: int = _parse_time_to_seconds(os.environ.get("CHAT_EXPIRE_TIME", "1h"))


class ChatModel(BaseModel):
    started_at: int
    history: List[ChatMessage] = []

    def __init__(self, **data: Any):
        super().__init__(**data)

    def add_user_message(self, prompt: str, bot_name: str, **kwargs) -> None:
        self.history.append(
            ChatMessage(
                role="user",
                blocks=[TextBlock(text=prompt)],
                metadata={**kwargs.pop("metadata", dict()), "bot_asked": bot_name},
                **kwargs
            )
        )

    def update_system_message(self, system_message: ChatMessage) -> "ChatModel":
        """Add the system message into the chat"""

        # Remove the old one
        ChatBuffer.pop_system(self.history)

        if system_message.role != "system":
            raise ValueError("Tried to update system message with non-system role.")

        # No messages yet
        self.history.insert(0, system_message)
        return self


class Chats(CacheObject):
    async def set(self, chat_id: str, chat_model: ChatModel, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.set(
                chat_id, chat_model.model_dump_json(), ex=kwargs.get('ex', CHAT_EXPIRE_TIME)
            )

    async def get(self, chat_id: str, **kwargs) -> Optional[ChatModel]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            result: Optional[bytes] = await redis.get(chat_id)

            if result is not None:
                data: dict = json.loads(result.decode("utf-8"))
                return ChatModel(**data)

            return None

    async def delete(self, chat_id: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(chat_id)

    async def exists(self, chat_id: str, **kwargs) -> bool:
        return bool(await self.get(chat_id=chat_id))
