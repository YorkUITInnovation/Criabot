from __future__ import annotations

import asyncio
import logging
import re
from typing import List

import httpx

from criabot.criadex_schemas import TextNodeWithScore


logger = logging.getLogger(__name__)


_FRENCH_MARKER_RE = re.compile(
    r"\b("
    r"bonjour|salut|merci|s'il\s+vous\s+plait|svp|recherche|chercher|trouver|"
    r"internet|ligne|fran[cç]ais|france|qu[e']|quoi|comment|pourquoi|"
    r"est-ce|avec|sans|dans|sur|vous|nous|ils|elles"
    r")\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ']+")
_EXPLICIT_WEB_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:search|look(?:\s+it)?\s+up|check)\s+(?:the\s+)?(?:web|internet|online)\s+(?:for|about)?\s+",
    re.IGNORECASE,
)
_EXPLICIT_WEB_PREFIX_FR_RE = re.compile(
    r"^\s*(?:cherche|recherche|trouve)\s+(?:sur\s+(?:le\s+)?web|sur\s+internet|en\s+ligne)\s+",
    re.IGNORECASE,
)
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
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 8.0,
        max_results: int = 5,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.35,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_results = max_results
        self._retry_attempts = max(0, int(retry_attempts))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    @staticmethod
    def _candidate_queries(query: str) -> List[str]:
        stripped_query = (query or "").strip()
        if not stripped_query:
            return []

        candidates = [stripped_query]
        for pattern in (_EXPLICIT_WEB_PREFIX_RE, _EXPLICIT_WEB_PREFIX_FR_RE):
            cleaned_query = pattern.sub("", stripped_query).strip(" .?")
            if cleaned_query and cleaned_query.lower() != stripped_query.lower():
                candidates.append(cleaned_query)

        return list(dict.fromkeys(candidates))

    @staticmethod
    def _candidate_languages(language: str) -> List[str]:
        normalized_language = (language or "").strip() or "en-US"
        candidates = [normalized_language]
        base_language = re.split(r"[-_]", normalized_language, maxsplit=1)[0].strip()
        if base_language and base_language.lower() != normalized_language.lower():
            candidates.append(base_language)
        return list(dict.fromkeys(candidates))

    async def _perform_search(self, query: str, language: str) -> List[dict]:
        params = {
            "q": query,
            "format": "json",
            "language": language,
            "safesearch": 1,
            "categories": "general",
        }
        headers = {
            "User-Agent": "CriaBot-WebSearch/1.0",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        }

        max_attempts = self._retry_attempts + 1
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=True) as client:
            for attempt in range(max_attempts):
                try:
                    response = await client.get(f"{self._base_url}/search", params=params, headers=headers)

                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(self._retry_backoff_seconds * (2 ** attempt))
                            continue
                        response.raise_for_status()

                    # Non-retriable client errors should not fail chat fallback flow.
                    if response.status_code >= 400:
                        return []

                    payload = response.json()
                    results = payload.get("results", [])
                    if not isinstance(results, list):
                        return []

                    return results[:self._max_results]
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(self._retry_backoff_seconds * (2 ** attempt))
                        continue
                except ValueError as exc:
                    last_error = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(self._retry_backoff_seconds * (2 ** attempt))
                        continue

        if last_error is not None:
            logger.warning("Web search request failed after retries: %s", last_error)
            raise last_error
        return []

    async def search(self, query: str, language: str | None = None) -> List[dict]:
        if not query.strip():
            return []

        effective_language = (language or infer_search_language(query)).strip()
        if not effective_language:
            effective_language = "en-US"
        if effective_language.lower() in {"fr-ca", "fr_fr", "fr-ca", "fr_ca"}:
            effective_language = "fr"

        attempts = [(candidate_query, effective_language) for candidate_query in self._candidate_queries(query)]
        for fallback_language in self._candidate_languages(effective_language)[1:]:
            for candidate_query in self._candidate_queries(query):
                attempts.append((candidate_query, fallback_language))

        last_error: httpx.HTTPError | None = None
        for candidate_query, candidate_language in attempts:
            try:
                results = await self._perform_search(candidate_query, candidate_language)
            except httpx.HTTPError as exc:
                last_error = exc
                continue

            if results:
                return results

        if last_error is not None:
            raise last_error

        return []


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
