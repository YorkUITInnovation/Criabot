import json
import os
from typing import List, Optional, Any

from redis import asyncio as aioredis
from criabot.criadex_schemas import ChatMessage, TextBlock
from pydantic import BaseModel

from criabot.bot.chat.buffer import ChatBuffer
from criabot.cache.core import CacheObject
from criabot.cache.ttl import parse_time_to_seconds


CHAT_EXPIRE_TIME: int = parse_time_to_seconds(os.environ.get("CHAT_EXPIRE_TIME", "1h"))
CACHE_TOUCH_ON_READ: bool = os.environ.get("CACHE_TOUCH_ON_READ", "true").lower() == "true"


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
    key_prefix = "chat:"

    async def set(self, chat_id: str, chat_model: ChatModel, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.set(
                self._key(chat_id), chat_model.model_dump_json(), ex=kwargs.get('ex', CHAT_EXPIRE_TIME)
            )

    async def get(self, chat_id: str, **kwargs) -> Optional[ChatModel]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            key = self._key(chat_id)
            ex = kwargs.get("ex", CHAT_EXPIRE_TIME)

            if kwargs.get("touch", CACHE_TOUCH_ON_READ):
                # GETEX atomically reads and resets TTL in one round-trip.
                result: Optional[bytes] = await redis.getex(key, ex=ex)
            else:
                result: Optional[bytes] = await redis.get(key)

            if result is None:
                return None

            data: dict = json.loads(result.decode("utf-8"))
            return ChatModel(**data)

    async def delete(self, chat_id: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(self._key(chat_id))

    async def exists(self, chat_id: str, **kwargs) -> bool:
        # Use a native EXISTS so we don't fetch + deserialize the full history
        # just to check presence.
        async with self.redis() as redis:
            redis: aioredis.Redis
            return bool(await redis.exists(self._key(chat_id)))
