from __future__ import annotations

from typing import List

import httpx

from CriadexSDK.ragflow_schemas import TextNodeWithScore


class WebSearchClient:
    def __init__(self, base_url: str, timeout_seconds: float = 8.0, max_results: int = 5) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results = max_results

    async def search(self, query: str) -> List[dict]:
        if not query.strip():
            return []

        params = {
            "q": query,
            "format": "json",
            "language": "en-US",
            "safesearch": 1,
            "categories": "general",
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"{self._base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()

        results = payload.get("results", [])
        if not isinstance(results, list):
            return []

        return results[:self._max_results]


def build_web_search_nodes(results: List[dict], group_name: str = "WEB_SEARCH") -> List[TextNodeWithScore]:
    nodes: List[TextNodeWithScore] = []
    rank = 0

    for result in results:
        if not isinstance(result, dict):
            continue

        title = str(result.get("title", "")).strip()
        url = str(result.get("url", "")).strip()
        content = str(result.get("content", "")).strip()

        if not (url and (title or content)):
            continue

        rank += 1
        excerpt = content if content else title
        text = f"[WEB RESULT #{rank}] {title}\nURL: {url}\nSnippet: {excerpt}".strip()

        score = max(0.1, 1.0 - ((rank - 1) * 0.1))
        nodes.append(
            TextNodeWithScore(
                node={
                    "metadata": {
                        "source_type": "web_search",
                        "source_url": url,
                        "title": title,
                        "group_name": group_name,
                        "file_name": title or url,
                    },
                    "excluded_embed_metadata_keys": [],
                    "excluded_llm_metadata_keys": [],
                    "class_name": "TextNode",
                    "text": text,
                    "text_template": "{metadata_str}\n\n{content}",
                    "metadata_template": "{key}: {value}",
                },
                score=score,
            )
        )

    return nodes
