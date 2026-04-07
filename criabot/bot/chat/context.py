import asyncio
import inspect
import itertools
import os
import re
import textwrap
from typing import List, Optional, Dict, Awaitable, Union, Type

from CriadexSDK.ragflow_sdk import RAGFlowSDK
from CriadexSDK.ragflow_schemas import TextNodeWithScore, Filter, GroupSearchResponse, CompletionUsage, Asset
from pydantic import BaseModel

from criabot.bot.bot import Bot
from criabot.bot.chat.buffer import History
from criabot.bot.chat.schemas import RelatedPrompt, Context, QuestionContext, TextContext
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
    _PROMPT_SPLIT_RE = re.compile(r",|\band\b", re.IGNORECASE)
    _PROMPT_PREFIX_RE = re.compile(
        r"^(give me|provide|create|write)\s+(a\s+)?(summary|response|answer)\s+(including|with)\s+",
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

    async def search_groups(
            self,
            prompt,
            metadata_filter,
            extra_bots
    ):
        async def search_named_group(group_name: str, search_config: dict):
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
                except Exception:
                    search_result = None

            if search_result is None:
                search_result = await self._criadex.content.search(
                    group_name=group_name,
                    search_config=search_config
                )
                if inspect.isawaitable(search_result):
                    search_result = await search_result
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
            else:
                if isinstance(search_result, GroupSearchResponse):
                    response_obj = search_result
                elif callable(getattr(search_result, "verify", None)):
                    verified = search_result.verify()
                    if inspect.isawaitable(verified):
                        verified = await verified
                    response_obj = getattr(verified, "response", verified)
                    if inspect.isawaitable(response_obj):
                        response_obj = await response_obj
                else:
                    raise TypeError("Unsupported search response payload type")
            if graph_meta:
                response_obj.metadata = {**(response_obj.metadata or {}), "graph_rag": graph_meta}
            return {"group_name": group_name, "response": response_obj}

        async def safe_search(group_name: str, search_config: dict):
            try:
                return await search_named_group(group_name=group_name, search_config=search_config)
            except Exception as e:
                # Missing index groups should not crash chat. Treat as "no context".
                message = str(e)
                if "GROUP_NOT_FOUND" in message or "Group not found" in message:
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
        faq_threshold = float(getattr(self._bot_params, "faq_fallback_threshold", self._faq_fallback_threshold))
        faq_enabled = bool(getattr(self._bot_params, "faq_fallback_enabled", self._faq_fallback_enabled))

        # If there are no nodes, or confidence is too low, try FAQ fallback first.
        if faq_enabled and (len(nodes) < 1 or max((float(node.score or 0.0) for node in nodes), default=0.0) < faq_threshold):
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

        # If there are no nodes after fallback attempt, return no-context.
        if len(nodes) < 1:
            return retriever_response
        ranked_nodes = self.normalize_ranked_nodes(nodes)
        if len(ranked_nodes) > 0:
            retriever_response.context = self.build_context(ranked_nodes=ranked_nodes)
        # Give 'er
        return retriever_response

    @classmethod
    def build_retrieval_prompts(cls, prompt: str) -> List[str]:
        prompts = [prompt.strip()]
        stripped_prompt = cls._PROMPT_PREFIX_RE.sub("", prompt.strip()).strip(" .")
        segments = [
            segment.strip(" .")
            for segment in cls._PROMPT_SPLIT_RE.split(stripped_prompt)
            if segment.strip(" .")
        ]

        for segment in segments:
            if len(segment) < 6:
                continue
            prompts.append(segment)

        return list(dict.fromkeys(prompts))

    def normalize_ranked_nodes(self, ranked_nodes: List[TextNodeWithScore]) -> List[TextNodeWithScore]:
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

        def sort_key(node: TextNodeWithScore) -> tuple[float, int, float]:
            metadata = node.node.metadata or {}
            group_name = metadata.get(self.GROUP_NAME_METADATA_KEY, "")
            own_groups = {
                self._bot.group_name(index_type)
                for index_type in self.INDEX_TYPES
            }
            source_priority = 0 if group_name in own_groups else 1
            score = float(node.score or 0.0)
            # Prefer the child's own groups when scores are effectively tied.
            return (-round(score, 2), source_priority, -score)

        return sorted(unique_nodes, key=sort_key)

    @classmethod
    def build_context(cls, ranked_nodes: List[TextNodeWithScore]) -> Union[QuestionContext, TextContext]:

        top_node_score: float = ranked_nodes[0].score
        top_node: TextNodeWithScore = ranked_nodes[0]
        # If there are multiple nodes with the top score
        # Make sure that a QUESTION
        for node in ranked_nodes:

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
                nodes=ranked_nodes,
                related_prompts=related_prompts,
            )

        # Case 2) Top node is not a question or direct response is not requested
        # This is the main case, text context gets built here
        return TextContext(
            text=build_text_context(nodes=ranked_nodes),
            nodes=ranked_nodes,
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
