import json
import os
from typing import List, Optional, Any

from redis import asyncio as aioredis
from pydantic import BaseModel

from criabot.cache.core import CacheObject
from criabot.cache.ttl import parse_time_to_seconds


GRADEBOOK_SESSION_EXPIRE_TIME: int = parse_time_to_seconds(
    os.environ.get("GRADEBOOK_SESSION_EXPIRE_TIME", "4h")
)
CACHE_TOUCH_ON_READ: bool = os.environ.get("CACHE_TOUCH_ON_READ", "true").lower() == "true"


class GradebookSessionModel(BaseModel):
    session_id: str
    course_id: str
    professor_id: str
    bot_name: str
    phase: str
    moodle_resources: List[dict] = []
    course_activities: List[dict] = []
    proposal: Optional[dict] = None
    extraction: Optional[dict] = None
    content_mapping: Optional[dict] = None
    last_touched_at: Optional[int] = None


class Gradebooks(CacheObject):
    key_prefix = "gb:"

    async def set(self, session_id: str, session_model: GradebookSessionModel, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.set(
                self._key(session_id),
                session_model.model_dump_json(),
                ex=kwargs.get('ex', GRADEBOOK_SESSION_EXPIRE_TIME)
            )

    async def get(self, session_id: str, **kwargs) -> Optional[GradebookSessionModel]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            key = self._key(session_id)
            ex = kwargs.get("ex", GRADEBOOK_SESSION_EXPIRE_TIME)

            if kwargs.get("touch", CACHE_TOUCH_ON_READ):
                # GETEX atomically reads and resets TTL in one round-trip.
                result: Optional[bytes] = await redis.getex(key, ex=ex)
            else:
                result: Optional[bytes] = await redis.get(key)

            if result is None:
                return None

            data = json.loads(result.decode('utf-8'))
            return GradebookSessionModel(**data)

    async def delete(self, session_id: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(self._key(session_id))

    async def exists(self, session_id: str, **kwargs) -> bool:
        # Native EXISTS avoids deserializing the whole session payload.
        async with self.redis() as redis:
            redis: aioredis.Redis
            return bool(await redis.exists(self._key(session_id)))
