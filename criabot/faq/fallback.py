from __future__ import annotations

import inspect
import os
from typing import Any, Dict, List

from CriadexSDK.ragflow_schemas import GroupSearchResponse


class FAQFallback:
    def __init__(self, criadex) -> None:
        self._criadex = criadex
        self._faq_group = os.environ.get("FAQ_GROUP_NAME", "eclass-faq-bot-document-index")
        self._graph_auto_build = os.environ.get("GRAPH_RAG_CHAT_AUTO_BUILD", "true").lower() == "true"

    async def search(self, prompt: str, top_k: int = 5) -> Dict[str, Any]:
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
            except Exception:
                result = None

        if result is None:
            result = await self._criadex.content.search(
                group_name=self._faq_group,
                search_config=search_config,
            )
            if inspect.isawaitable(result):
                result = await result

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

        return {
            "group_name": self._faq_group,
            "response": response_obj,
            "sources": deduped,
            "graph_metadata": graph_meta,
        }
