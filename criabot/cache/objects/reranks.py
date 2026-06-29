import json
import logging
import os
from typing import List, Optional

from redis import asyncio as aioredis

from criabot.cache.core import CacheObject
from criabot.cache.helpers import fingerprint_rerank_nodes, normalize_cache_text, stable_hash

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


RERANK_CACHE_TTL_SECONDS: int = _parse_int_env("RERANK_CACHE_TTL_SECONDS", 600)


class Reranks(CacheObject):
    """Caches rerank agent output keyed by prompt + model + node fingerprints."""

    key_prefix = "rerank:"

    @staticmethod
    def build_key(
        *,
        prompt: str,
        rerank_model_id: int,
        top_n: int,
        min_n: int,
        nodes,
    ) -> str:
        return stable_hash(
            normalize_cache_text(prompt),
            str(rerank_model_id),
            str(top_n),
            str(min_n),
            fingerprint_rerank_nodes(nodes),
        )

    async def set(self, cache_key: str, ranked_nodes_payload: List[dict], **kwargs) -> None:
        ttl = kwargs.get("ex", RERANK_CACHE_TTL_SECONDS)
        async with self.redis() as redis:
            await redis.set(self._key(cache_key), json.dumps(ranked_nodes_payload), ex=ttl)

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
