from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Set
from urllib.parse import urljoin, urlparse

import httpx


class _LinkAndTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: Set[str] = set()
        self._text_chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.add(href.strip())

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self._text_chunks.append(data.strip())

    @property
    def text(self) -> str:
        return "\n".join(chunk for chunk in self._text_chunks if chunk)


class FAQCrawler:
    """
    Crawl FAQ/help pages from a single host and extract text content.
    """

    def __init__(self, timeout_seconds: float = 20.0, client_factory=None) -> None:
        self._timeout = timeout_seconds
        self._client_factory = client_factory or httpx.AsyncClient

    async def crawl(self, source_url: str, max_pages: int = 25) -> list[dict]:
        base_host = urlparse(source_url).netloc
        if not base_host:
            raise ValueError("Invalid source URL")

        visited: Set[str] = set()
        queue: List[str] = [source_url]
        pages: list[dict] = []

        async with self._client_factory(timeout=self._timeout, follow_redirects=True) as client:
            while queue and len(pages) < max_pages:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                # Network hiccups or slow pages should not abort the whole FAQ sync.
                try:
                    response = await client.get(url)
                except httpx.RequestError:
                    continue
                if response.status_code != 200:
                    continue
                content_type = (response.headers.get("content-type") or "").lower()
                if "text/html" not in content_type:
                    continue

                parser = _LinkAndTextParser()
                parser.feed(response.text)
                text = parser.text.strip()
                if text:
                    pages.append({
                        "url": url,
                        "title": urlparse(url).path or "/",
                        "text": text,
                    })

                for href in parser.links:
                    absolute_url = urljoin(url, href)
                    parsed = urlparse(absolute_url)
                    if parsed.netloc != base_host:
                        continue
                    if parsed.scheme not in {"http", "https"}:
                        continue
                    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if normalized not in visited and normalized not in queue:
                        queue.append(normalized)

        return pages
