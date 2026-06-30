from __future__ import annotations

import inspect
import logging
import os
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from criabot.criadex_schemas import GroupSearchResponse

if TYPE_CHECKING:
    from criabot.cache.objects.faq_searches import FaqSearches

logger = logging.getLogger(__name__)


class FAQFallback:
    # Process-local L1 cache (fast path); Redis L2 is optional via faq_cache.
    _cache: Dict[str, tuple[int, Dict[str, Any]]] = {}

    def __init__(self, criadex, faq_cache: Optional["FaqSearches"] = None) -> None:
        self._criadex = criadex
        self._faq_cache = faq_cache
        self._faq_group = os.environ.get("FAQ_GROUP_NAME", "eclass-faq-bot-document-index")
        self._graph_auto_build = os.getenv("GRAPH_RAG_CHAT_AUTO_BUILD", "true").lower() == "true"
        self._cache_ttl_seconds = int(os.environ.get("FAQ_FALLBACK_CACHE_SECONDS", "300"))
        self._missing_group_cache_ttl_seconds = int(
            os.environ.get("FAQ_FALLBACK_MISSING_GROUP_CACHE_SECONDS", "1800")
        )
        self._redis_cache_enabled = os.getenv("FAQ_REDIS_CACHE_ENABLED", "true").lower() == "true"

    @staticmethod
    def _is_group_missing_error(exc: Exception) -> bool:
        msg = str(exc)
        status_code = getattr(exc, "status_code", None)
        return (
            status_code == 404
            or "GROUP_NOT_FOUND" in msg
            or "Group not found" in msg
            or "INDEX_NOT_FOUND" in msg
        )

    @staticmethod
    def _empty_group_response_payload() -> Dict[str, Any]:
        return {
            "response": {
                "nodes": [],
                "assets": [],
                "search_units": 0,
                "metadata": {},
            }
        }

    def _memory_cache_key(self, prompt: str, top_k: int) -> str:
        return f"{self._faq_group}:{top_k}:{prompt.strip().lower()}"

    def _redis_cache_key(self, prompt: str, top_k: int) -> Optional[str]:
        if self._faq_cache is None or not self._redis_cache_enabled:
            return None
        from criabot.cache.objects.faq_searches import FaqSearches
        return FaqSearches.build_key(faq_group=self._faq_group, prompt=prompt, top_k=top_k)

    async def _get_cached(self, prompt: str, top_k: int) -> Optional[Dict[str, Any]]:
        memory_key = self._memory_cache_key(prompt, top_k)
        cached = self._cache.get(memory_key)
        if cached and cached[0] > int(time.time()):
            return cached[1]

        redis_key = self._redis_cache_key(prompt, top_k)
        if redis_key is None:
            return None
        try:
            payload = await self._faq_cache.get(redis_key)
        except Exception:
            logger.debug("FAQ Redis cache read failed", exc_info=True)
            return None
        if payload is not None:
            self._cache[memory_key] = (int(time.time()) + self._cache_ttl_seconds, payload)
        return payload

    async def _store_cached(
        self,
        prompt: str,
        top_k: int,
        payload: Dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        memory_key = self._memory_cache_key(prompt, top_k)
        self._cache[memory_key] = (int(time.time()) + ttl_seconds, payload)

        redis_key = self._redis_cache_key(prompt, top_k)
        if redis_key is None:
            return
        try:
            await self._faq_cache.set(redis_key, payload, ex=ttl_seconds)
        except Exception:
            logger.debug("FAQ Redis cache write failed", exc_info=True)

    async def search(self, prompt: str, top_k: int = 5) -> Dict[str, Any]:
        cached_payload = await self._get_cached(prompt, top_k)
        if cached_payload is not None:
            return cached_payload

        search_config = {"query": prompt, "top_k": top_k}

        result = None
        manage_api = getattr(self._criadex, "manage", None)
        graph_search = getattr(manage_api, "graph_search", None) if manage_api is not None else None
        if callable(graph_search):
            try:
                result = await graph_search(
                    group_name=self._faq_group,
                    search_config={
                        **search_config,
                        "max_hops": 1,
                        "max_expansion_terms": 8,
                        "auto_build": self._graph_auto_build,
                    },
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                if self._is_group_missing_error(exc):
                    empty_payload = {
                        "group_name": self._faq_group,
                        "response": GroupSearchResponse(**self._empty_group_response_payload()["response"]),
                        "sources": [],
                        "graph_metadata": None,
                    }
                    await self._store_cached(
                        prompt,
                        top_k,
                        empty_payload,
                        ttl_seconds=self._missing_group_cache_ttl_seconds,
                    )
                    return empty_payload
                result = None

        if result is None:
            try:
                result = await self._criadex.content.search(
                    group_name=self._faq_group,
                    search_config=search_config,
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                if self._is_group_missing_error(exc):
                    empty_payload = {
                        "group_name": self._faq_group,
                        "response": GroupSearchResponse(**self._empty_group_response_payload()["response"]),
                        "sources": [],
                        "graph_metadata": None,
                    }
                    await self._store_cached(
                        prompt,
                        top_k,
                        empty_payload,
                        ttl_seconds=self._missing_group_cache_ttl_seconds,
                    )
                    return empty_payload
                raise

        if isinstance(result, dict):
            payload = result.get("response", result)
            if not isinstance(payload, dict):
                payload = result
            response_obj = GroupSearchResponse(**payload)
            graph_meta = result.get("graph_metadata")
        else:
            response_obj = result
            graph_meta = None

        sources: List[dict] = []
        for node in response_obj.nodes:
            md = node.node.metadata or {}
            url = md.get("source_url") or md.get("url")
            if not url:
                continue
            sources.append(
                {
                    "type": "eclass_faq",
                    "title": md.get("title") or md.get("file_name") or "FAQ Source",
                    "url": url,
                    "category": md.get("category"),
                    "confidence": float(node.score or 0.0),
                }
            )

        deduped: List[dict] = []
        seen_urls = set()
        for source in sources:
            if source["url"] in seen_urls:
                continue
            seen_urls.add(source["url"])
            deduped.append(source)

        response_payload = {
            "group_name": self._faq_group,
            "response": response_obj,
            "sources": deduped,
            "graph_metadata": graph_meta,
        }
        await self._store_cached(
            prompt,
            top_k,
            response_payload,
            ttl_seconds=self._cache_ttl_seconds,
        )
        return response_payload
