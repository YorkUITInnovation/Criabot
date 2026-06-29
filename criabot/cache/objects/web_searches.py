import hashlib
import json
import logging
import os
from typing import List, Optional

from redis import asyncio as aioredis

from criabot.cache.core import CacheObject

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Web search results are expensive (SearXNG + scraping) and reasonably stable
# over short windows, so a short TTL gives a good cost/freshness trade-off.
# Plain integer seconds avoids the months-vs-minutes ambiguity of the duration
# parser used for chat/gradebook sessions.
WEB_SEARCH_CACHE_TTL_SECONDS: int = _parse_int_env("WEB_SEARCH_CACHE_TTL_SECONDS", 900)


class WebSearches(CacheObject):
    """Caches serialized web-search result nodes keyed by normalized query."""

    key_prefix = "websearch:"

    @staticmethod
    def build_key(query: str, language: str, max_results: int) -> str:
        normalized_query = " ".join((query or "").strip().lower().split())
        normalized_language = (language or "").strip().lower()
        raw = f"{normalized_query}|{normalized_language}|{max_results}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def set(self, cache_key: str, nodes_payload: List[dict], **kwargs) -> None:
        ttl = kwargs.get("ex", WEB_SEARCH_CACHE_TTL_SECONDS)
        async with self.redis() as redis:
            await redis.set(self._key(cache_key), json.dumps(nodes_payload), ex=ttl)

    async def get(self, cache_key: str, **kwargs) -> Optional[List[dict]]:
        async with self.redis() as redis:
            redis: aioredis.Redis
            result: Optional[bytes] = await redis.get(self._key(cache_key))
            if result is None:
                return None
            return json.loads(result.decode("utf-8"))

    async def delete(self, cache_key: str, **kwargs) -> None:
        async with self.redis() as redis:
            await redis.delete(self._key(cache_key))

    async def exists(self, cache_key: str, **kwargs) -> bool:
        async with self.redis() as redis:
            redis: aioredis.Redis
            return bool(await redis.exists(self._key(cache_key)))
