import asyncio
import inspect
import itertools
import os
import re
import textwrap
from typing import List, Optional, Dict, Awaitable, Union, Type

from CriadexSDK.ragflow_sdk import RAGFlowSDK, CriadexNetworkError, CriadexAPIError
from CriadexSDK.ragflow_schemas import TextNodeWithScore, Filter, GroupSearchResponse, CompletionUsage, Asset
from pydantic import BaseModel

from criabot.bot.bot import Bot
from criabot.bot.chat.buffer import History
from criabot.bot.chat.schemas import RelatedPrompt, Context, QuestionContext, TextContext
from criabot.bot.chat.web_search import WebSearchClient, build_web_search_nodes, infer_search_language
from criabot.database.bots.tables.bot_params import BotParametersModel
from criabot.faq.fallback import FAQFallback

GroupSearchResponses: Type = Dict[str, GroupSearchResponse]


class ContextRetrieverResponse(BaseModel):
    context: object = None
    group_responses: dict = None
    token_usage: list = []
    search_units: int = 0
    faq_fallback_used: bool = False
    faq_sources: List[dict] = []
    indexing_in_progress: bool = False
    indexing_groups: List[str] = []

    @classmethod
    def get_search_units(cls, group_responses):
        search_units: int = 0

        for group_response in group_responses.values():
            search_units += group_response.search_units

        return search_units

    @property
    def nodes(self) -> List[TextNodeWithScore]:
        return list(
            itertools.chain.from_iterable(r.nodes for r in self.group_responses.values())
        )

    @property
    def assets(self) -> List[Asset]:
        return list(
            itertools.chain.from_iterable(r.assets for r in self.group_responses.values())
        )


class ContextRetriever:
    INDEX_TYPES = ["DOCUMENT", "QUESTION"]
    FILE_NAME_METADATA_KEY: str = "file_name"
    LLM_REPLY_METADATA_KEY: str = "llm_reply"
    GROUP_NAME_METADATA_KEY: str = "group_name"
    ANSWER_METADATA_KEY: str = "answer"
    RELATED_PROMPTS_METADATA_KEY: str = "related_prompts"
    PROBE_FALLBACK_METADATA_KEY: str = "_probe_fallback"
    _PROMPT_SPLIT_RE = re.compile(r",|\band\b", re.IGNORECASE)
    _PROMPT_PREFIX_RE = re.compile(
        r"^(give me|provide|create|write)\s+(a\s+)?(summary|response|answer)\s+(including|with)\s+",
        re.IGNORECASE,
    )
    _QUESTION_PREFIX_RE = re.compile(
        r"^(what|when|where|who|which|why|how)\s+"
        r"(is|was|are|were|do|does|did|can|could|should|would|will|has|have|had)\s+",
        re.IGNORECASE,
    )
    _WEB_SEARCH_HINT_RE = re.compile(
        r"\b("
        r"search\s+the\s+web|search\s+online|web\s+search|look\s+it\s+up\s+online|from\s+the\s+web|"
        r"search\s+internet|on\s+the\s+web|"
        r"cherche\s+sur\s+le\s+web|recherche\s+sur\s+internet|sur\s+le\s+web|en\s+ligne"
        r")\b",
        re.IGNORECASE,
    )
    _TIME_SENSITIVE_RE = re.compile(
        r"\b("
        r"latest|recent|today|now|current|news|update|updates|this\s+week|this\s+month|"
        r"breaking|live|status|price|weather|release\s+notes|"
        r"dernier|derniere|dernieres|actuel|actuelle|aujourd'hui|mise\s+a\s+jour|nouvelles"
        r")\b",
        re.IGNORECASE,
    )

    def __init__(
            self,
            criadex,
            rerank_model_id,
            llm_model_id,
            bot,
            bot_params
    ):
        self._criadex = criadex
        self._rerank_model_id = rerank_model_id
        self._llm_model_id = llm_model_id
        self._bot = bot
        self._bot_params = bot_params
        self._graph_enabled = os.getenv("GRAPH_RAG_CHAT_ENABLED", "true").lower() == "true"
        self._graph_auto_build = os.getenv("GRAPH_RAG_CHAT_AUTO_BUILD", "true").lower() == "true"
        self._faq_fallback_enabled = os.getenv("FAQ_FALLBACK_ENABLED", "true").lower() == "true"
        self._faq_fallback_threshold = float(os.getenv("FAQ_FALLBACK_THRESHOLD", "0.5"))
        self._web_search_global_enabled = os.getenv("WEB_SEARCH_GLOBAL_ENABLED", "true").lower() == "true"
        self._web_search_url = os.getenv("WEB_SEARCH_URL", "http://searxng:8080")
        self._web_search_timeout_seconds = float(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "8"))
        self._web_search_max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
        self._web_search_fallback_only = os.getenv("WEB_SEARCH_FALLBACK_ONLY", "true").lower() == "true"
        self._web_search_fallback_threshold = float(
            os.getenv("WEB_SEARCH_FALLBACK_SCORE_THRESHOLD", str(self._faq_fallback_threshold))
        )
        self._known_confidence_threshold = float(os.getenv("RETRIEVAL_KNOWN_THRESHOLD", "0.75"))
        self._semi_known_confidence_threshold = float(os.getenv("RETRIEVAL_SEMI_THRESHOLD", "0.4"))
        self._indexing_retry_attempts = int(os.getenv("RETRIEVAL_INDEXING_RETRY_ATTEMPTS", "3"))
        self._indexing_retry_delay_seconds = float(os.getenv("RETRIEVAL_INDEXING_RETRY_DELAY_SECONDS", "1.0"))

    @staticmethod
    def _extract_listed_file_count(list_payload: object) -> int:
        if isinstance(list_payload, dict):
            for key in ("files", "document_names", "documents"):
                value = list_payload.get(key)
                if isinstance(value, list):
                    return len(value)

            response = list_payload.get("response")
            if isinstance(response, dict):
                for key in ("files", "document_names", "documents"):
                    value = response.get(key)
                    if isinstance(value, list):
                        return len(value)

        return 0

    async def _group_file_count(self, group_name: str) -> int:
        try:
            list_payload = await self._criadex.content.list(group_name=group_name)
            if inspect.isawaitable(list_payload):
                list_payload = await list_payload
            return self._extract_listed_file_count(list_payload)
        except Exception:
            return 0

    @staticmethod
    def _set_indexing_metadata(response_obj: GroupSearchResponse, group_name: str, file_count: int) -> GroupSearchResponse:
        response_obj.metadata = {
            **(response_obj.metadata or {}),
            "indexing_in_progress": True,
            "group_name": group_name,
            "listed_file_count": file_count,
        }
        return response_obj

    @classmethod
    def _explicit_web_search_requested(cls, prompt: str) -> bool:
        return bool(cls._WEB_SEARCH_HINT_RE.search(prompt or ""))

    @classmethod
    def _is_summary_style_request(cls, prompt: str) -> bool:
        return bool(cls._PROMPT_PREFIX_RE.match((prompt or "").strip()))

    @classmethod
    def _is_time_sensitive_request(cls, prompt: str) -> bool:
        return bool(cls._TIME_SENSITIVE_RE.search(prompt or ""))

    def _classify_retrieval_mode(self, prompt: str, max_node_score: float) -> str:
        # Lightweight routing classifier for retrieval orchestration.
        if self._is_time_sensitive_request(prompt):
            return "external"
        if max_node_score > self._known_confidence_threshold:
            return "known"
        if max_node_score > self._semi_known_confidence_threshold:
            return "semi_known"
        return "unknown"

    @classmethod
    def _extract_focused_question_prompt(cls, prompt: str) -> Optional[str]:
        stripped_prompt = (prompt or "").strip().strip(" .?")
        if not stripped_prompt:
            return None

        focused_prompt = cls._QUESTION_PREFIX_RE.sub("", stripped_prompt).strip(" .?")
        if len(focused_prompt) < 6 or focused_prompt.lower() == stripped_prompt.lower():
            return None

        return focused_prompt

    @staticmethod
    def _strip_leading_article(prompt: str) -> str:
        return re.sub(r"^(the|a|an)\s+", "", (prompt or "").strip(), flags=re.IGNORECASE)

    @classmethod
    def _prompt_keywords(cls, prompt: str) -> List[str]:
        tokens = re.findall(r"[A-Za-z0-9']+", (prompt or "").lower())
        stopwords = {
            "the", "is", "was", "are", "were", "a", "an", "and", "or", "to", "of",
            "in", "on", "for", "with", "me", "give", "provide", "create", "write",
            "what", "when", "where", "who", "which", "why", "how",
        }
        keywords = [token for token in tokens if token not in stopwords and len(token) > 1]
        return list(dict.fromkeys(keywords))

    @classmethod
    def prioritize_nodes_for_prompt(cls, prompt: str, nodes: List[TextNodeWithScore]) -> List[TextNodeWithScore]:
        focused_prompt = cls._extract_focused_question_prompt(prompt) or prompt
        keywords = cls._prompt_keywords(focused_prompt)
        if not keywords:
            return nodes

        def relevance_key(item: tuple[int, TextNodeWithScore]) -> tuple[int, int, float, int]:
            index, node = item
            text = (node.node.text or "").lower()
            matched_keywords = sum(1 for keyword in keywords if keyword in text)
            total_matches = sum(text.count(keyword) for keyword in keywords)
            return (-matched_keywords, -total_matches, -float(node.score or 0.0), index)

        return [node for _, node in sorted(enumerate(nodes), key=relevance_key)]

    def _can_use_web_search(self) -> bool:
        global_setting = bool(getattr(self._bot_params, "web_search_global_enabled", True))
        bot_setting = bool(getattr(self._bot_params, "web_search_enabled", False))
        return self._web_search_global_enabled and global_setting and bot_setting

    async def _search_web_nodes(self, prompt: str) -> List[TextNodeWithScore]:
        client = WebSearchClient(
            base_url=self._web_search_url,
            timeout_seconds=self._web_search_timeout_seconds,
            max_results=self._web_search_max_results,
        )
        results = await client.search(prompt, language=infer_search_language(prompt))
        return build_web_search_nodes(results)

    @staticmethod
    def _attach_web_group_response(
            retriever_response: ContextRetrieverResponse,
            web_nodes: List[TextNodeWithScore],
    ) -> None:
        retriever_response.group_responses = retriever_response.group_responses or {}
        retriever_response.group_responses["WEB_SEARCH"] = GroupSearchResponse(
            nodes=web_nodes or [],
            assets=[],
            search_units=0,
            metadata={"group_name": "WEB_SEARCH", "source_type": "web_search"},
        )
        retriever_response.search_units = ContextRetrieverResponse.get_search_units(retriever_response.group_responses)

    @staticmethod
    def _looks_like_group_search_result(payload: object) -> bool:
        if isinstance(payload, GroupSearchResponse):
            return True
        if isinstance(payload, dict):
            if any(key in payload for key in ("nodes", "assets", "search_units")):
                return True
            for key in ("response", "result", "data"):
                nested = payload.get(key)
                if isinstance(nested, dict) and any(k in nested for k in ("nodes", "assets", "search_units")):
                    return True
            return False
        return False

    @staticmethod
    def _result_has_nodes(payload: object) -> bool:
        if isinstance(payload, GroupSearchResponse):
            return bool(payload.nodes)
        if isinstance(payload, dict):
            if isinstance(payload.get("nodes"), list):
                return len(payload.get("nodes") or []) > 0
            for key in ("response", "result", "data"):
                nested = payload.get(key)
                if isinstance(nested, dict) and isinstance(nested.get("nodes"), list):
                    return len(nested.get("nodes") or []) > 0
        return False

    @classmethod
    def _mark_probe_fallback_nodes(cls, response_obj: GroupSearchResponse) -> GroupSearchResponse:
        for node in response_obj.nodes:
            metadata = dict(node.node.metadata or {})
            metadata[cls.PROBE_FALLBACK_METADATA_KEY] = True
            node.node.metadata = metadata
        return response_obj

    async def search_groups(
            self,
            prompt,
            metadata_filter,
            extra_bots
    ):
        def to_group_response(search_result: object) -> tuple[GroupSearchResponse, object]:
            graph_meta = None
            if isinstance(search_result, dict):
                candidate = None
                for key in ("response", "result", "data"):
                    value = search_result.get(key)
                    if isinstance(value, dict) and (
                        "nodes" in value or "assets" in value or "search_units" in value
                    ):
                        candidate = value
                        break
                if candidate is None:
                    if any(k in search_result for k in ("nodes", "assets", "search_units")):
                        candidate = search_result
                    else:
                        candidate = search_result.get("response", search_result)
                response_obj = (
                    candidate
                    if isinstance(candidate, GroupSearchResponse)
                    else GroupSearchResponse(**candidate)
                )
                if isinstance(candidate, dict):
                    graph_meta = candidate.get("graph_metadata") or search_result.get("graph_metadata")
                return response_obj, graph_meta

            if isinstance(search_result, GroupSearchResponse):
                return search_result, graph_meta

            if callable(getattr(search_result, "verify", None)):
                verified = search_result.verify()
                if inspect.isawaitable(verified):
                    # Keep sync helper pure; awaitable case handled by caller before conversion.
                    raise TypeError("Awaitable verify() payload must be resolved before conversion")
                response_obj = getattr(verified, "response", verified)
                if inspect.isawaitable(response_obj):
                    raise TypeError("Awaitable response payload must be resolved before conversion")
                return response_obj, graph_meta

            raise TypeError("Unsupported search response payload type")

        async def search_named_group(group_name: str, search_config: dict):
            def build_keyword_query(raw_prompt: str) -> str:
                tokens = re.findall(r"[A-Za-z0-9']+", raw_prompt or "")
                keywords = [token for token in tokens if len(token) > 2 or token.isdigit()]
                # Keep deterministic and compact to avoid noisy expansions.
                return " ".join(keywords[:12]).strip()

            search_result = None
            graph_meta = None
            if self._graph_enabled:
                try:
                    manage_api = getattr(self._criadex, "manage", None)
                    graph_search = getattr(manage_api, "graph_search", None) if manage_api is not None else None
                    if callable(graph_search):
                        graph_payload = {
                            **search_config,
                            "max_hops": 1,
                            "max_expansion_terms": 8,
                            "auto_build": self._graph_auto_build,
                        }
                        search_result = await graph_search(
                            group_name=group_name,
                            search_config=graph_payload
                        )
                        if inspect.isawaitable(search_result):
                            search_result = await search_result
                        if not self._looks_like_group_search_result(search_result):
                            search_result = None
                        elif not self._result_has_nodes(search_result):
                            # If graph search yields an empty response, fall back to standard retrieval.
                            search_result = None
                except Exception:
                    search_result = None

            if search_result is None:
                search_result = await self._criadex.content.search(
                    group_name=group_name,
                    search_config=search_config
                )
                if inspect.isawaitable(search_result):
                    search_result = await search_result
            response_obj, graph_meta_from_result = to_group_response(search_result)
            if graph_meta_from_result:
                graph_meta = graph_meta_from_result

            # Intermittent ANN/search behavior can return 0 nodes for one source while
            # peers return results. Retry once with a broader config for this group.
            if not response_obj.nodes:
                broad_search_config = {
                    **search_config,
                    "top_k": max(int(search_config.get("top_k", 0) or 0), 50),
                    "top_n": max(int(search_config.get("top_n", 0) or 0), 20),
                    "min_k": 0.0,
                    "min_n": 0.0,
                }
                retry_result = await self._criadex.content.search(
                    group_name=group_name,
                    search_config=broad_search_config,
                )
                if inspect.isawaitable(retry_result):
                    retry_result = await retry_result
                retry_response_obj, retry_graph_meta = to_group_response(retry_result)
                if retry_response_obj.nodes:
                    response_obj = retry_response_obj
                    if retry_graph_meta:
                        graph_meta = retry_graph_meta

            # Final fallback for prompt-specific misses: retry with compact keyword query.
            if not response_obj.nodes:
                keyword_query = build_keyword_query(search_config.get("query", ""))
                if keyword_query:
                    keyword_search_config = {
                        **search_config,
                        "query": keyword_query,
                        "top_k": max(int(search_config.get("top_k", 0) or 0), 80),
                        "top_n": max(int(search_config.get("top_n", 0) or 0), 30),
                        "min_k": 0.0,
                        "min_n": 0.0,
                    }
                    keyword_result = await self._criadex.content.search(
                        group_name=group_name,
                        search_config=keyword_search_config,
                    )
                    if inspect.isawaitable(keyword_result):
                        keyword_result = await keyword_result
                    keyword_response_obj, keyword_graph_meta = to_group_response(keyword_result)
                    if keyword_response_obj.nodes:
                        response_obj = keyword_response_obj
                        if keyword_graph_meta:
                            graph_meta = keyword_graph_meta

            # Last-resort for document groups: force a lightweight lexical probe to
            # avoid returning an empty source when a group definitely has content.
            if not response_obj.nodes and group_name.endswith("-document-index"):
                probe_search_config = {
                    **search_config,
                    "query": "the",
                    "top_k": 1,
                    "top_n": 1,
                    "min_k": 0.0,
                    "min_n": 0.0,
                }
                probe_result = await self._criadex.content.search(
                    group_name=group_name,
                    search_config=probe_search_config,
                )
                if inspect.isawaitable(probe_result):
                    probe_result = await probe_result
                probe_response_obj, probe_graph_meta = to_group_response(probe_result)
                if probe_response_obj.nodes:
                    probe_response_obj = self._mark_probe_fallback_nodes(probe_response_obj)
                    response_obj = probe_response_obj
                    if probe_graph_meta:
                        graph_meta = probe_graph_meta

            if graph_meta:
                response_obj.metadata = {**(response_obj.metadata or {}), "graph_rag": graph_meta}

            # Fresh uploads may be listed in-group before retrieval is queryable.
            # Retry briefly and mark as indexing so callers can return a specific status.
            if not response_obj.nodes and group_name.endswith("-document-index"):
                attempts = max(self._indexing_retry_attempts, 0)
                file_count = 0
                for attempt in range(attempts):
                    file_count = await self._group_file_count(group_name=group_name)
                    if file_count < 1:
                        break

                    if attempt > 0:
                        await asyncio.sleep(self._indexing_retry_delay_seconds)

                    retry_result = await self._criadex.content.search(
                        group_name=group_name,
                        search_config={
                            **search_config,
                            "top_k": max(int(search_config.get("top_k", 0) or 0), 80),
                            "top_n": max(int(search_config.get("top_n", 0) or 0), 30),
                            "min_k": 0.0,
                            "min_n": 0.0,
                        },
                    )
                    if inspect.isawaitable(retry_result):
                        retry_result = await retry_result
                    retry_response_obj, retry_graph_meta = to_group_response(retry_result)
                    if retry_response_obj.nodes:
                        response_obj = retry_response_obj
                        if retry_graph_meta:
                            response_obj.metadata = {
                                **(response_obj.metadata or {}),
                                "graph_rag": retry_graph_meta,
                            }
                        break

                if not response_obj.nodes and file_count > 0:
                    response_obj = self._set_indexing_metadata(
                        response_obj=response_obj,
                        group_name=group_name,
                        file_count=file_count,
                    )

            return {"group_name": group_name, "response": response_obj}

        async def safe_search(group_name: str, search_config: dict):
            try:
                return await search_named_group(group_name=group_name, search_config=search_config)
            except Exception as e:
                # Missing index groups should not crash chat. Treat as "no context".
                message = str(e)
                if (
                    isinstance(e, CriadexNetworkError)
                    or (isinstance(e, CriadexAPIError) and getattr(e, "status_code", None) in (404, 503, 504))
                    or "GROUP_NOT_FOUND" in message
                    or "Group not found" in message
                    or "INDEX_NOT_FOUND" in message
                    or "Name or service not known" in message
                    or "Network error after" in message
                ):
                    return None
                raise

        tasks = []
        for index_type in self.INDEX_TYPES:
            search_config = self.build_search_group_config(
                prompt=prompt,
                metadata_filter=metadata_filter,
                extra_groups=[]
            )
            group_names = [
                self._bot.group_name(index_type),
                *[Bot.bot_group_name(extra_bot, index_type) for extra_bot in extra_bots],
            ]
            for group_name in dict.fromkeys(group_names):
                tasks.append(safe_search(group_name=group_name, search_config=search_config))

        results = await asyncio.gather(*tasks)
        results = [r for r in results if r is not None]
        return {
            (r["group_name"] if isinstance(r, dict) else r.group_name): (r["response"] if isinstance(r, dict) else r.response)
            for r in results
        }

    async def hybrid_rerank(
            self,
            prompt,
            nodes,
    ):
        response = await self._criadex.agents.cohere.rerank(
            model_id=self._rerank_model_id,
            agent_config={
                "prompt": prompt,
                "nodes": [node.model_dump(mode='json') for node in nodes],
                "top_n": self._bot_params.top_n,
                "min_n": self._bot_params.min_n
            }
        )

        reranked_docs = response.get("reranked_documents", [])
        if reranked_docs and isinstance(reranked_docs[0], dict):
            reranked_docs = [TextNodeWithScore(**doc) for doc in reranked_docs]

        return {
            "ranked_nodes": reranked_docs,
            "search_units": 0
        }

    def build_search_group_config(
            self,
            prompt,
            metadata_filter,
            extra_groups
    ):
        return {
            "query": prompt,
            "top_k": self._bot_params.top_k,
            "min_k": self._bot_params.min_k,
            "top_n": self._bot_params.top_n,
            "min_n": self._bot_params.min_n,
            "search_filter": metadata_filter,
            "extra_groups": extra_groups,
        }

    async def transform_prompt(self, prompt, history):
        response = await self._criadex.agents.azure.transform(
            model_id=self._llm_model_id,
            agent_config={
                "prompt": prompt,
                "history": history
            }
        )
        return response["agent_response"] if isinstance(response, dict) else response.verify().agent_response

    @classmethod
    def merge_responses(
            cls,
            *response_lists
    ):
        output = {}
        for response_list in response_lists:
            for name, index_response in response_list.items():
                if name not in output:
                    output[name] = index_response
                else:
                    output[name].nodes.extend(index_response.nodes)
                    output[name].search_units += index_response.search_units
                    output[name].metadata = {**output[name].metadata, **index_response.metadata}
        return output

    @classmethod
    def is_first_prompt(cls, history):
        return len(history) <= 2

    async def retrieve(
            self,
            prompt,
            metadata_filter,
            extra_bots
    ):
        retriever_response = ContextRetrieverResponse(
            group_responses={}
        )
        search_prompts = self.build_retrieval_prompts(prompt)
        summary_style_request = self._is_summary_style_request(prompt)
        response_sets = []
        for search_prompt in search_prompts:
            response_sets.append(
                await self.search_groups(
                    prompt=search_prompt,
                    metadata_filter=metadata_filter,
                    extra_bots=extra_bots
                )
            )

        group_responses = self.merge_responses(*response_sets) if response_sets else {}
        retriever_response.search_units = ContextRetrieverResponse.get_search_units(group_responses)
        retriever_response.group_responses = group_responses
        nodes = retriever_response.nodes
        max_node_score = max((float(node.score or 0.0) for node in nodes), default=0.0)
        web_search_requested = self._explicit_web_search_requested(prompt)
        web_search_enabled = self._can_use_web_search()
        retrieval_mode = self._classify_retrieval_mode(prompt=prompt, max_node_score=max_node_score)
        faq_threshold = float(getattr(self._bot_params, "faq_fallback_threshold", self._faq_fallback_threshold))
        web_threshold = float(self._web_search_fallback_threshold)
        faq_enabled = bool(getattr(self._bot_params, "faq_fallback_enabled", self._faq_fallback_enabled))

        # If there are no nodes, or confidence is too low, try FAQ fallback first.
        if faq_enabled and (len(nodes) < 1 or max_node_score < faq_threshold):
            try:
                fallback = FAQFallback(criadex=self._criadex)
                fallback_result = await fallback.search(prompt=prompt, top_k=max(3, self._bot_params.top_n))
                fallback_response = fallback_result.get("response")
                if isinstance(fallback_response, GroupSearchResponse) and fallback_response.nodes:
                    retriever_response.group_responses[fallback_result["group_name"]] = fallback_response
                    retriever_response.search_units = ContextRetrieverResponse.get_search_units(retriever_response.group_responses)
                    retriever_response.faq_fallback_used = True
                    retriever_response.faq_sources = fallback_result.get("sources", [])
                    retriever_response.context = TextContext(
                        text=build_text_context(nodes=fallback_response.nodes),
                        nodes=fallback_response.nodes,
                        related_prompts=[],
                    )
                    return retriever_response
            except Exception:
                pass

        should_run_web_parallel = web_search_enabled and (
            web_search_requested or retrieval_mode == "external"
        )
        should_run_web_fallback = web_search_enabled and (
            retrieval_mode == "unknown" or len(nodes) < 1 or max_node_score < web_threshold
        )

        if should_run_web_fallback or should_run_web_parallel:
            if should_run_web_parallel or not self._web_search_fallback_only:
                try:
                    web_nodes = await self._search_web_nodes(prompt)
                    if web_nodes:
                        self._attach_web_group_response(retriever_response, web_nodes)
                        nodes = self.normalize_ranked_nodes([*nodes, *web_nodes])
                        if nodes:
                            retriever_response.context = TextContext(
                                text=build_text_context(nodes=nodes),
                                nodes=nodes,
                                related_prompts=[],
                            )
                            return retriever_response
                except Exception:
                    pass
            else:
                try:
                    web_nodes = await self._search_web_nodes(prompt)
                    if web_nodes:
                        self._attach_web_group_response(retriever_response, web_nodes)
                        retriever_response.context = TextContext(
                            text=build_text_context(nodes=web_nodes),
                            nodes=web_nodes,
                            related_prompts=[],
                        )
                        return retriever_response
                except Exception:
                    pass

        # If there are no nodes after fallback attempt, return no-context.
        if len(nodes) < 1:
            indexing_groups: List[str] = []
            for group_name, group_response in retriever_response.group_responses.items():
                if not isinstance(group_response, GroupSearchResponse):
                    continue
                metadata = group_response.metadata or {}
                if metadata.get("indexing_in_progress"):
                    indexing_groups.append(group_name)

            if indexing_groups:
                retriever_response.indexing_in_progress = True
                retriever_response.indexing_groups = indexing_groups
            return retriever_response
        ranked_nodes: List[TextNodeWithScore] = []
        try:
            rerank_result = await self.hybrid_rerank(prompt=prompt, nodes=nodes)
            ranked_nodes = rerank_result.get("ranked_nodes") or []
        except Exception:
            ranked_nodes = []

        ranked_nodes = self.normalize_ranked_nodes(
            ranked_nodes or nodes,
            blend_sources=summary_style_request,
            preserve_order=bool(ranked_nodes),
        )
        if not summary_style_request:
            ranked_nodes = self.prioritize_nodes_for_prompt(prompt, ranked_nodes)
            ranked_nodes = ranked_nodes[:3]
        if len(ranked_nodes) > 0:
            retriever_response.context = self.build_context(ranked_nodes=ranked_nodes)
        # Give 'er
        return retriever_response

    @classmethod
    def build_retrieval_prompts(cls, prompt: str) -> List[str]:
        prompts = [prompt.strip()]
        stripped_prompt = prompt.strip()

        if cls._is_summary_style_request(prompt):
            summary_prompt = cls._PROMPT_PREFIX_RE.sub("", stripped_prompt).strip(" .")
            segments = [
                segment.strip(" .")
                for segment in cls._PROMPT_SPLIT_RE.split(summary_prompt)
                if segment.strip(" .")
            ]

            for segment in segments:
                if len(segment) < 6:
                    continue
                prompts.append(segment)
        else:
            focused_prompt = cls._extract_focused_question_prompt(stripped_prompt)
            if focused_prompt:
                prompts.append(focused_prompt)
                article_free_prompt = cls._strip_leading_article(focused_prompt)
                if article_free_prompt and article_free_prompt.lower() != focused_prompt.lower():
                    prompts.append(article_free_prompt)

        return list(dict.fromkeys(prompts))

    def normalize_ranked_nodes(
        self,
        ranked_nodes: List[TextNodeWithScore],
        blend_sources: bool = False,
        preserve_order: bool = False,
    ) -> List[TextNodeWithScore]:
        unique_nodes: list[TextNodeWithScore] = []
        seen: set[tuple[str, str, str]] = set()

        for node in ranked_nodes:
            metadata = node.node.metadata or {}
            dedupe_key = (
                metadata.get(self.GROUP_NAME_METADATA_KEY, ""),
                metadata.get(self.FILE_NAME_METADATA_KEY, ""),
                node.node.text,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            unique_nodes.append(node)

        def sort_key(node: TextNodeWithScore) -> tuple[float, float]:
            metadata = node.node.metadata or {}
            probe_priority = 1 if metadata.get(self.PROBE_FALLBACK_METADATA_KEY) else 0
            score = float(node.score or 0.0)
            # Prefer real query hits over probe fallbacks and otherwise stay
            # score-first. Source ownership is not a reliable proxy for relevance.
            return (probe_priority, -score)

        if preserve_order:
            non_probe_nodes = [
                node for node in unique_nodes
                if not (node.node.metadata or {}).get(self.PROBE_FALLBACK_METADATA_KEY)
            ]
            probe_nodes = [
                node for node in unique_nodes
                if (node.node.metadata or {}).get(self.PROBE_FALLBACK_METADATA_KEY)
            ]
            sorted_nodes = [*non_probe_nodes, *probe_nodes]
        else:
            sorted_nodes = sorted(unique_nodes, key=sort_key)
        
        if not blend_sources:
            return sorted_nodes

        # Multi-source blending is only useful for explicit summary-style prompts.
        # Single-fact questions should stay relevance-first to avoid distracting context.
        sources_by_group: Dict[str, list[TextNodeWithScore]] = {}
        for node in sorted_nodes:
            metadata = node.node.metadata or {}
            group_name = metadata.get(self.GROUP_NAME_METADATA_KEY, "unknown")
            if group_name not in sources_by_group:
                sources_by_group[group_name] = []
            sources_by_group[group_name].append(node)
        
        # If we have multiple sources, ensure each source is represented in results.
        # No minimum node count required - we want diversity even with sparse results.
        if len(sources_by_group) > 1:
            result = []
            # First pass: add top node from each source to guarantee representation
            for group_name in sorted(sources_by_group.keys()):
                if sources_by_group[group_name]:
                    result.append(sources_by_group[group_name][0])
            # Second pass: add remaining nodes in score order, up to reasonable limit
            for node in sorted_nodes:
                if len(result) >= 10:  # cap total nodes
                    break
                if node not in result:
                    result.append(node)
            return result
        
        return sorted_nodes

    @classmethod
    def build_context(cls, ranked_nodes: List[TextNodeWithScore]) -> Union[QuestionContext, TextContext]:
        # Prefer document-like nodes when available. Question-index nodes can be noisy
        # and may override better factual context from parent/child document sources.
        non_question_nodes = [node for node in ranked_nodes if not cls.is_question_node(node)]
        candidate_nodes = non_question_nodes or ranked_nodes

        top_node_score: float = candidate_nodes[0].score
        top_node: TextNodeWithScore = candidate_nodes[0]
        # If there are multiple nodes with the top score
        # Make sure that a QUESTION
        for node in candidate_nodes:

            if node.score > top_node_score:
                top_node = node

        related_prompts: List[RelatedPrompt] = []

        # Case 1) Top node is a question & direct response is requested
        if cls.is_question_node(top_node):
            related_prompts = top_node.node.metadata.get(cls.RELATED_PROMPTS_METADATA_KEY) or []

            # LLM Reply NOT Enabled
            if not cls.is_llm_reply(top_node):
                return QuestionContext(
                    file_name=top_node.node.metadata.get(cls.FILE_NAME_METADATA_KEY),
                    group_name=top_node.node.metadata.get(cls.GROUP_NAME_METADATA_KEY),
                    node=top_node,
                    related_prompts=related_prompts
                )

            # LLM Reply Enabled
            # Note: This change will reduce accuracy by cutting out relevant nodes if the top node is a question
            # Was asked to make this change. The problem with this approach will be that if people ask 2-part questions, or generally if the answer would benefit
            # from multiple nodes, it will only return the Q answer. Or if there is an issue with re-ranking, it will only return the incorrect Q answer.
            # It's a cost-benefit of whether the potential for hallucination is worth the potential for better overall answers.
            top_node.node.metadata.get(cls.ANSWER_METADATA_KEY)
            return TextContext(
                text=build_text_context(nodes=[top_node]),
                nodes=candidate_nodes,
                related_prompts=related_prompts,
            )

        # Case 2) Top node is not a question or direct response is not requested
        # This is the main case, text context gets built here
        return TextContext(
            text=build_text_context(nodes=candidate_nodes),
            nodes=candidate_nodes,
            related_prompts=related_prompts
        )

    @classmethod
    def is_question_node(cls, node: TextNodeWithScore) -> bool:

        return (
                cls.ANSWER_METADATA_KEY in node.node.metadata
                and cls.LLM_REPLY_METADATA_KEY in node.node.metadata
        )

    @classmethod
    def is_llm_reply(cls, node: TextNodeWithScore) -> bool:

        if not cls.is_question_node(node):
            return False

        return node.node.metadata.get(cls.LLM_REPLY_METADATA_KEY)


def build_text_context(nodes: List[TextNodeWithScore]) -> str:
    """
    Build context given a set of relevant nodes

    :param nodes: The relevant nodes
    :return: The context string

    """

    context: List[str] = []

    for idx, node in enumerate(nodes):
        context.append(f"[DOCUMENT #{idx + 1}]\n" + node.node.text)

    return "\n\n".join(context)


_RE_COMBINE_MULTISPACE = re.compile(r" +")


def clean_text(text: str) -> str:
    return _RE_COMBINE_MULTISPACE.sub(" ", textwrap.dedent(text)).strip()


def build_context_prompt(context: TextContext, prompt: str = "", best_guess: bool = False) -> str:
    """
    Build a context-enabled prompt given the components

    :param context: The associated context
    :param best_guess: Whether to use best guess if irrelevant content is ranked
    :return: The context prompt

    """

    extra_text: str = (
        "If nothing from this information is relevant, use your knowledge to guess."
        if best_guess else
        "If nothing from this information is relevant, say your database don't have that information, even if you do have a guess."
    )

    return clean_text(
        f"""
        [INSTRUCTIONS]
        Answer the user's latest question using the information below.
        If the information contains the answer, state that answer directly.
        Do not replace retrieved facts with outside knowledge, generic examples, or guesses.
        Quote or restate the retrieved facts when possible.

        The documents below are the top results returned from a search engine.
        They may be relevant or completely irrelevant to the question.
       
        IMPORTANT: If you use ANY information from an IMAGE DESCRIPTION, ALWAYS EMBED THE IMAGE as part of your answer using the format ![Asset](<image_id>),
        where <image_id> is a placeholder for the uuid found in the image description start/end tags. ONLY include the raw UUID, NEVER a URL.
        The ID of an image is found in the tags at the start and end of its description in the context below.
        A description tag looks like this: [IMAGE <image_id> DESCRIPTION START].
                
        {extra_text}

        [QUESTION]
        {prompt}

        [INFORMATION]
        {context.text}
        """
    )


def build_no_context_guess_prompt(no_context_message: Optional[str]) -> str:
    if no_context_message is not None:
        no_context_message = no_context_message.replace('\n', '')

        return textwrap.dedent(
            f"""
            [EXTRA INSTRUCTIONS]
            
            No information was found regarding the following question.
            The user was already sent the message "{no_context_message}" to let them know this.

            Use your knowledge to suggest what you think. Make sure you say it's a guess.
            Start your reply with a conjunction, like "However", or "But", and attempt to make a guess.
            """
        )

    return textwrap.dedent(
        """
        [EXTRA INSTRUCTIONS]
        
        No information was found regarding the following question.
        Use your knowledge to suggest what you think. Make sure you say it's a guess.
        """
    )


def build_no_context_llm_prompt() -> str:
    return textwrap.dedent(
        """
        [EXTRA INSTRUCTIONS]\n
        No information was found regarding the following question.\n
        Respond that you do not know the answer, taking the question into account.
        """
    )
