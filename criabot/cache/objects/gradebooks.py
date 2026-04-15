import json
import os
import re
from typing import List, Optional, Any

from redis import asyncio as aioredis
from pydantic import BaseModel

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


GRADEBOOK_SESSION_EXPIRE_TIME: int = _parse_time_to_seconds(
    os.environ.get("GRADEBOOK_SESSION_EXPIRE_TIME", "4h")
)


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


class Gradebooks(CacheObject):
    async def set(self, session_id: str, session_model: GradebookSessionModel, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.set(
                session_id,
                session_model.model_dump_json(),
                ex=kwargs.get('ex', GRADEBOOK_SESSION_EXPIRE_TIME)
            )

    async def get(self, session_id: str, **kwargs) -> Optional[GradebookSessionModel]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            result: Optional[bytes] = await redis.get(session_id)
            if result is None:
                return None
            data = json.loads(result.decode('utf-8'))
            return GradebookSessionModel(**data)

    async def delete(self, session_id: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(session_id)

    async def exists(self, session_id: str, **kwargs) -> bool:
        return bool(await self.get(session_id=session_id))
