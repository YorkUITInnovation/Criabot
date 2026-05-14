from __future__ import annotations

import re
from typing import List

import httpx

from CriadexSDK.ragflow_schemas import TextNodeWithScore


_FRENCH_MARKER_RE = re.compile(
    r"\b("
    r"bonjour|salut|merci|s'il\s+vous\s+plait|svp|recherche|chercher|trouver|"
    r"internet|ligne|fran[cç]ais|france|qu[e']|quoi|comment|pourquoi|"
    r"est-ce|avec|sans|dans|sur|vous|nous|ils|elles"
    r")\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ']+")
_FRENCH_STOPWORDS = {
    "le", "la", "les", "de", "des", "du", "et", "ou", "en", "sur", "avec", "sans", "pour",
    "est", "sont", "dans", "que", "qui", "quoi", "comment", "pourquoi", "quel", "quelle", "quels",
    "quelles", "bonjour", "merci", "recherche", "chercher", "internet", "ligne", "francais", "français",
}


def infer_search_language(query: str, default: str = "en-US") -> str:
    text = (query or "").strip()
    if not text:
        return default

    lowered = text.lower()
    if any(ch in lowered for ch in ("é", "è", "ê", "ë", "à", "â", "î", "ï", "ô", "ù", "û", "ç")):
        return "fr"
    if _FRENCH_MARKER_RE.search(lowered):
        return "fr"
    tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
    if sum(1 for token in tokens if token in _FRENCH_STOPWORDS) >= 2:
        return "fr"
    return default


class WebSearchClient:
    def __init__(self, base_url: str, timeout_seconds: float = 8.0, max_results: int = 5) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results = max_results

    async def search(self, query: str, language: str | None = None) -> List[dict]:
        if not query.strip():
            return []

        effective_language = (language or infer_search_language(query)).strip()
        if not effective_language:
            effective_language = "en-US"
        if effective_language.lower() in {"fr-ca", "fr_fr", "fr-ca", "fr_ca"}:
            effective_language = "fr"

        params = {
            "q": query,
            "format": "json",
            "language": effective_language,
            "safesearch": 1,
            "categories": "general",
        }
        headers = {
            "User-Agent": "CriaBot-WebSearch/1.0",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=True) as client:
            response = await client.get(f"{self._base_url}/search", params=params, headers=headers)
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
