import json
import logging
import os
from typing import Any, Dict, Optional

from redis import asyncio as aioredis
from criabot.criadex_schemas import GroupSearchResponse

from criabot.cache.core import CacheObject
from criabot.cache.helpers import normalize_cache_text, stable_hash

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


FAQ_SEARCH_CACHE_TTL_SECONDS: int = _parse_int_env("FAQ_FALLBACK_CACHE_SECONDS", 300)
FAQ_MISSING_GROUP_CACHE_TTL_SECONDS: int = _parse_int_env(
    "FAQ_FALLBACK_MISSING_GROUP_CACHE_SECONDS", 1800
)


class FaqSearches(CacheObject):
    """Caches FAQ fallback search payloads (shared across workers)."""

    key_prefix = "faq:"

    @staticmethod
    def build_key(*, faq_group: str, prompt: str, top_k: int) -> str:
        return stable_hash(faq_group, str(top_k), normalize_cache_text(prompt))

    @staticmethod
    def serialize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        serialized = dict(payload)
        response = serialized.get("response")
        if isinstance(response, GroupSearchResponse):
            serialized["response"] = response.model_dump(mode="json")
        return serialized

    @staticmethod
    def deserialize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        restored = dict(payload)
        response = restored.get("response")
        if isinstance(response, dict):
            restored["response"] = GroupSearchResponse(**response)
        return restored

    async def set(self, cache_key: str, payload: Dict[str, Any], **kwargs) -> None:
        ttl = kwargs.get("ex", FAQ_SEARCH_CACHE_TTL_SECONDS)
        async with self.redis() as redis:
            await redis.set(
                self._key(cache_key),
                json.dumps(self.serialize_payload(payload)),
                ex=ttl,
            )

    async def get(self, cache_key: str, **kwargs) -> Optional[Dict[str, Any]]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            result: Optional[bytes] = await redis.get(self._key(cache_key))
            if result is None:
                return None
            return self.deserialize_payload(json.loads(result.decode("utf-8")))

    async def delete(self, cache_key: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(self._key(cache_key))

    async def exists(self, cache_key: str, **kwargs) -> bool:
        async with self.redis() as redis:
            redis: aioredis.Redis
            return bool(await redis.exists(self._key(cache_key)))
