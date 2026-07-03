"""
Local Criadex HTTP client — replaces the CriadexSDK.ragflow_sdk dependency.

This module owns the async httpx client that Criabot uses to call Criadex's
REST API (groups, content, auth, models, agents). It is NOT the Ragflow native
SDK; Ragflow calls are made by Criadex internally using ragflow-sdk.
"""

from typing import Any, Optional
import asyncio
import json
import logging

from httpx import AsyncClient, Timeout, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)


class CriadexSDKError(Exception):
    """Base exception for Criadex client errors."""


class CriadexNetworkError(CriadexSDKError):
    """Network/connection errors."""


class CriadexAPIError(CriadexSDKError):
    """API errors (4xx, 5xx)."""

    def __init__(self, status_code: int, message: str, response: Optional[dict] = None) -> None:
        self.status_code = status_code
        self.message = message
        self.response = response
        super().__init__(f"[{status_code}] {message}")


async def _request_with_retry(
    httpx_client: AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int,
    **kwargs: Any,
) -> dict:
    last_exception: Optional[BaseException] = None

    for attempt in range(max_retries):
        try:
            logger.debug("CriadexClient request %s %s (attempt %d)", method, url, attempt + 1)
            resp = await httpx_client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except HTTPStatusError as exc:
            status = exc.response.status_code
            last_exception = exc
            if 400 <= status < 500:
                raise CriadexAPIError(status_code=status, message=exc.response.text) from exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise CriadexAPIError(
                status_code=status,
                message=f"Server error after {max_retries} attempts",
            ) from exc
        except RequestError as exc:
            last_exception = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise CriadexNetworkError(f"Network error after {max_retries} attempts: {exc}") from exc

    raise CriadexNetworkError(f"Request failed after {max_retries} attempts: {last_exception}")


class ContentRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def upload(self, group_name, file):
        url = f"{self._api_base}/groups/{group_name}/content/upload"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=file)

    async def search(self, group_name, search_config):
        url = f"{self._api_base}/groups/{group_name}/query"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=search_config)

    async def update(self, group_name, file):
        url = f"{self._api_base}/groups/{group_name}/content/update"
        return await _request_with_retry(self._httpx, "PATCH", url, max_retries=self._max_retries, json=file)

    async def delete(self, group_name, document_name):
        url = f"{self._api_base}/groups/{group_name}/content/delete"
        return await _request_with_retry(self._httpx, "DELETE", url, max_retries=self._max_retries, params={"document_name": document_name})

    async def list(self, group_name):
        url = f"{self._api_base}/groups/{group_name}/content/list"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def upload_file(self, group_name: str, filename: str, file_bytes: bytes, content_type: str = "application/octet-stream", strategy: Optional[str] = None):
        """Upload a raw file to Criadex's native Ragflow parse endpoint."""
        url = f"{self._api_base}/groups/{group_name}/content/upload/file"
        files = {"file": (filename, file_bytes, content_type)}
        data = {"strategy": strategy} if strategy else {}
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, files=files, data=data)


class GroupsRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def create(self, group_name, group_config):
        url = f"{self._api_base}/groups/{group_name}/create"
        dump = group_config.model_dump() if hasattr(group_config, "model_dump") else group_config
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=dump)

    async def delete(self, group_name):
        url = f"{self._api_base}/groups/{group_name}/delete"
        return await _request_with_retry(self._httpx, "DELETE", url, max_retries=self._max_retries)

    async def about(self, group_name):
        url = f"{self._api_base}/groups/{group_name}/about"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def build_graph(self, group_name):
        url = f"{self._api_base}/groups/{group_name}/build_graph"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries)

    async def graph_status(self, group_name):
        url = f"{self._api_base}/groups/{group_name}/graph_status"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def graph_search(self, group_name, search_config):
        url = f"{self._api_base}/groups/{group_name}/graph_search"
        dump = search_config.model_dump() if hasattr(search_config, "model_dump") else search_config
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=dump)


class AuthRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def create(self, api_key, create_config):
        url = f"{self._api_base}/auth/{api_key}/create"
        dump = create_config.model_dump() if hasattr(create_config, "model_dump") else create_config
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=dump)

    async def delete(self, api_key):
        url = f"{self._api_base}/auth/{api_key}/delete"
        return await _request_with_retry(self._httpx, "DELETE", url, max_retries=self._max_retries)

    async def check(self, api_key):
        url = f"{self._api_base}/auth/{api_key}/check"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def reset(self, api_key, new_key):
        url = f"{self._api_base}/auth/{api_key}/reset"
        return await _request_with_retry(self._httpx, "PATCH", url, max_retries=self._max_retries, json={"new_key": new_key})


class GroupAuthRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def create(self, group_name, api_key):
        url = f"{self._api_base}/group_auth/{group_name}/create"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, params={"api_key": api_key})

    async def check(self, group_name, api_key):
        url = f"{self._api_base}/group_auth/{group_name}/check"
        try:
            return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries, params={"api_key": api_key})
        except CriadexAPIError as exc:
            # Criadex returns authorized/master in the body even on 404 (GROUP_NOT_FOUND).
            # Parse the body so callers read authorized=null (falsy) instead of getting a crash.
            try:
                return json.loads(exc.message)
            except Exception:
                return {"authorized": False, "master": False}

    async def delete(self, group_name, api_key):
        url = f"{self._api_base}/group_auth/{group_name}/delete"
        return await _request_with_retry(self._httpx, "DELETE", url, max_retries=self._max_retries, params={"api_key": api_key})

    async def list(self, api_key):
        url = f"{self._api_base}/auth/keys/{api_key}/groups"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)


class ModelsRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def create(self, model_id, model_config, provider_type: str = "azure"):
        url = f"{self._api_base}/models/{provider_type}/create"
        dump = model_config.model_dump(mode="json") if hasattr(model_config, "model_dump") else dict(model_config)
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json={"model_id": model_id, **dump})

    async def delete(self, model_id, provider_type: str = "azure"):
        url = f"{self._api_base}/models/{provider_type}/{model_id}/delete"
        return await _request_with_retry(self._httpx, "DELETE", url, max_retries=self._max_retries)

    async def about(self, model_id, provider_type: str = "azure"):
        url = f"{self._api_base}/models/{provider_type}/{model_id}/about"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def list(self, provider_type: str = ""):
        url = f"{self._api_base}/models/{provider_type}/list" if provider_type else f"{self._api_base}/models/list"
        return await _request_with_retry(self._httpx, "GET", url, max_retries=self._max_retries)

    async def sync_ragflow(self):
        url = f"{self._api_base}/models/ragflow/sync"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries)

    async def update(self, model_id, model_config, provider_type: str = "azure"):
        url = f"{self._api_base}/models/{provider_type}/{model_id}/update"
        dump = model_config.model_dump(mode="json") if hasattr(model_config, "model_dump") else dict(model_config)
        return await _request_with_retry(self._httpx, "PATCH", url, max_retries=self._max_retries, json=dump)


class _AzureAgentsRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def chat(self, model_id, agent_config):
        url = f"{self._api_base}/models/ragflow/{model_id}/agents/chat"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=agent_config)

    async def related_prompts(self, model_id, agent_config):
        url = f"{self._api_base}/models/{model_id}/related_prompts"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=agent_config)

    async def transform(self, model_id, agent_config):
        url = f"{self._api_base}/models/ragflow/{model_id}/agents/transform"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=agent_config)

    async def intents(self, model_id, agent_config):
        url = f"{self._api_base}/models/ragflow/{model_id}/agents/intents"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=agent_config)

    async def language(self, model_id, agent_config):
        url = f"{self._api_base}/models/ragflow/{model_id}/agents/language"
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=agent_config)

    async def ensure_dialog(self, chat_id: str, model_id: Optional[str] = None, tenant_id: Optional[str] = None):
        url = f"{self._api_base}/ragflow/chats/{chat_id}/ensure"
        payload: dict = {}
        if model_id:
            payload["llm_id"] = model_id
        if tenant_id:
            payload["tenant_id"] = tenant_id
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=payload)


class _CohereAgentsRouter:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self._api_base = api_base
        self._httpx = httpx_client
        self._max_retries = max_retries

    async def rerank(self, model_id, agent_config):
        url = f"{self._api_base}/models/{model_id}/rerank"
        cfg = agent_config.model_dump(mode="json") if hasattr(agent_config, "model_dump") else dict(agent_config)
        payload: dict = {}
        payload["query"] = cfg.get("query") or cfg.get("prompt")
        payload["documents"] = cfg.get("documents") or cfg.get("nodes", [])
        for key in ("top_n", "min_n"):
            if key in cfg:
                payload[key] = cfg[key]
        headers = {"x-api-key": self._httpx.headers.get("x-api-key", "")}
        return await _request_with_retry(self._httpx, "POST", url, max_retries=self._max_retries, json=payload, headers=headers)


class _AgentsNamespace:
    def __init__(self, api_base: str, httpx_client: AsyncClient, max_retries: int) -> None:
        self.azure = _AzureAgentsRouter(api_base, httpx_client, max_retries)
        self.cohere = _CohereAgentsRouter(api_base, httpx_client, max_retries)


class RAGFlowSDK:
    """Async Criadex HTTP client. Replaces CriadexSDK.ragflow_sdk.RAGFlowSDK."""

    def __init__(self, api_base: str, error_stacktrace: bool = True, timeout: float = 30.0, max_retries: int = 3):
        self._api_base = api_base.rstrip("/")
        self._error_stacktrace = error_stacktrace
        self._timeout = Timeout(timeout)
        self._max_retries = max_retries
        self._httpx = AsyncClient(timeout=self._timeout)
        if "Authorization" in self._httpx.headers:
            del self._httpx.headers["Authorization"]
        self.content = ContentRouter(self._api_base, self._httpx, self._max_retries)
        self.manage = GroupsRouter(self._api_base, self._httpx, self._max_retries)
        self.auth = AuthRouter(self._api_base, self._httpx, self._max_retries)
        self.group_auth = GroupAuthRouter(self._api_base, self._httpx, self._max_retries)
        self.models = ModelsRouter(self._api_base, self._httpx, self._max_retries)
        self.agents = _AgentsNamespace(self._api_base, self._httpx, self._max_retries)

    def authenticate(self, api_key: str) -> None:
        self._httpx.headers["x-api-key"] = api_key
        if "Authorization" in self._httpx.headers:
            del self._httpx.headers["Authorization"]
