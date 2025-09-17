import asyncio
import itertools
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

GroupSearchResponses: Type = Dict[str, GroupSearchResponse]


class ContextRetrieverResponse(BaseModel):
    context: object = None
    group_responses: dict = None
    token_usage: list = []
    search_units: int = 0

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

    async def search_groups(
            self,
            prompt,
            metadata_filter,
            extra_bots
    ):
        index_queries = []

        for index_type in self.INDEX_TYPES:
            search_config = self.build_search_group_config(
                prompt=prompt,
                metadata_filter=metadata_filter,
                extra_groups=[Bot.bot_group_name(extra_bot, index_type) for extra_bot in extra_bots]
            )
            index_queries.append(
                self._bot.search_group(index_type=index_type, search_config=search_config)
            )

        results = await asyncio.gather(*index_queries)
        return {r["group_name"] if isinstance(r, dict) else r.group_name: r["response"] if isinstance(r, dict) else r.response for r in results}

    async def hybrid_rerank(
            self,
            prompt,
            nodes,
    ):
        response = await self._criadex.agents.cohere.rerank(
            model_id=self._rerank_model_id,
            agent_config={
                "prompt": prompt,
                "nodes": nodes,
                "top_n": self._bot_params.top_n,
                "min_n": self._bot_params.min_n
            }
        )
        return response["agent_response"] if isinstance(response, dict) else response.verify().agent_response

    def build_search_group_config(
            self,
            prompt,
            metadata_filter,
            extra_groups
    ):
        return {
            "prompt": prompt,
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
        # Retrieve using original prompt
        group_responses = await self.search_groups(
            prompt=prompt,
            metadata_filter=metadata_filter,
            extra_bots=extra_bots
        )
        retriever_response.search_units = ContextRetrieverResponse.get_search_units(group_responses)
        retriever_response.group_responses = group_responses
        nodes = retriever_response.nodes
        # If there are no nodes
        if len(nodes) < 1:
            return retriever_response
        # Execute hybrid re-rank
        rerank_response = await self.hybrid_rerank(
            prompt=prompt,
            nodes=nodes
        )
        retriever_response.search_units += rerank_response.search_units
        # Make sure we have something
        if len(rerank_response.ranked_nodes) > 0:
            retriever_response.context = self.build_context(
                ranked_nodes=rerank_response.ranked_nodes,
            )
        # Give 'er
        return retriever_response

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


def build_context_prompt(context: TextContext, best_guess: bool = False) -> str:
    """
    Build a context-enabled prompt given the components

    :param context: The associated context
    :param best_guess: Whether to use best guess if irrelevant content is ranked
    :return: The context prompt

    """

    extra_text: str = (
        """If nothing from this information is relevant, use your knowledge to guess."""
        if best_guess else
        """If nothing from this information is relevant, say your database don't have that information,
         even if you do have a guess."""
    )

    return clean_text(
        f"""
        [INSTRUCTIONS]
        The documents below are the top results returned from a search engine.
        They may be relevant or completely irrelevant to the question.
       
        IMPORTANT: If you use ANY information from an IMAGE DESCRIPTION, ALWAYS EMBED THE IMAGE as part of your answer using the format ![Asset](<image_id>),
        where <image_id> is a placeholder for the uuid found in the image description start/end tags. ONLY include the raw UUID, NEVER a URL.
        The ID of an image is found in the tags at the start and end of its description in the context below.
        A description tag looks like this: [IMAGE <image_id> DESCRIPTION START].
                
        {extra_text}

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
