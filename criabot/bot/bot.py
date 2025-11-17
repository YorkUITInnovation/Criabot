import time
import asyncio
import uuid
import logging
import json
from typing import Dict, Awaitable, Callable

from CriadexSDK.ragflow_sdk import RAGFlowSDK
from CriadexSDK.ragflow_schemas import GroupSearchResponse

from criabot.bot.schemas import GroupContentResponse
from criabot.cache.api import BotCacheAPI
from criabot.cache.objects.chats import ChatModel


class Bot:
    # This must NOT be changed lol
    INDEX_SUFFIX: Dict[str, str] = {
        "QUESTION": "-question-index",
        "DOCUMENT": "-document-index",
        # "CACHE": "-cache-index"
    }

    def __init__(
            self,
            name: str,
            criadex,
            bot_cache: BotCacheAPI
    ):
        self._name: str = name
        self._criadex = criadex
        self._cache_api: BotCacheAPI = bot_cache
        self.intents = [
            {"name": "Greeting", "description": "User says hello"},
            {"name": "Question", "description": "User asks a question"},
            {"name": "Farewell", "description": "User says goodbye"}
        ]

    @property
    def criadex(self):
        return self._criadex

    @property
    def cache_api(self) -> BotCacheAPI:
        return self._cache_api

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    async def start_chat(cls, cache_api: BotCacheAPI) -> str:
        """
        Add a new chat to the cache and return the ID. Users should insert the system message
        as the FIRST message in history

        :return: The new chat ID

        """

        chat_id: str = str(uuid.uuid4())

        # Create a chat object
        chat_model: ChatModel = ChatModel(
            started_at=round(time.time()),
        )

        # Insert the object into the cache
        await cls._set_chat_model(
            cache_api=cache_api,
            chat_id=chat_id,
            chat_model=chat_model
        )

        return chat_id

    @classmethod
    async def _set_chat_model(
            cls,
            cache_api: BotCacheAPI,
            chat_id: str,
            chat_model: ChatModel
    ) -> None:
        """
          Set a chat in the redis config

          :param chat_id: The ID of the chat
          :param chat_model: Its contents (config + history)
          :return: None

          """
        await cache_api.chats.set(chat_id=chat_id, chat_model=chat_model)

    async def set_chat_model(self, chat_id: str, chat_model: ChatModel) -> None:
        """
        Set a chat in the redis config

        :param chat_id: The ID of the chat
        :param chat_model: Its contents (config + history)
        :return: None

        """

        await self._set_chat_model(
            cache_api=self._cache_api,
            chat_id=chat_id,
            chat_model=chat_model
        )

    def group_name(self, index_type) -> str:
        """
        Get the index name from the index type

        :param index_type: Type of index
        :return: Its name

        """

        return self.bot_group_name(self._name, index_type)

    @classmethod
    def bot_group_name(cls, bot_name: str, index_type) -> str:
        """
        Get the name of a given index for a bot

        """

        return bot_name + cls.INDEX_SUFFIX[index_type]

    async def search_group(
        self,
        index_type,
        search_config
    ):
        """
        Ask a documents on one of the Bot's indexes

        :param index_type: The type of index to query
        :param search_config: The config for searching the index via Criadex
        :return: Vector DB Response

        """
        group_name = self.group_name(index_type)
        # Ensure we await the SDK call (it is async) and support both
        # dict and pydantic-style responses.
        search_result = await self._criadex.content.search(
            group_name=group_name,
            search_config=search_config
        )
        if isinstance(search_result, dict):
            # Some backends return the payload nested under 'response', others
            # return the payload at the root, and some use 'result'/'data'.
            # Extract flexibly and fall back to the root dict if it already
            # looks like a GroupSearchResponse payload.
            candidate = None
            for key in ("response", "result", "data"):
                value = search_result.get(key)
                if isinstance(value, dict) and ("nodes" in value or "assets" in value or "search_units" in value):
                    candidate = value
                    break
            if candidate is None:
                # If the root dict already contains the expected fields, use it
                if any(k in search_result for k in ("nodes", "assets", "search_units")):
                    candidate = search_result
                else:
                    candidate = search_result.get("response", search_result)

            response_obj = (
                candidate
                if isinstance(candidate, GroupSearchResponse)
                else GroupSearchResponse(**candidate)
            )
        else:
            # Assume SDK returned a model-like object with .verify()/.response
            verified = getattr(search_result, 'verify', lambda: search_result)()
            response_obj = getattr(verified, 'response', verified)
        return {"group_name": group_name, "response": response_obj}

    async def retrieve_group_info(self):
        """
        Retrieve the LLM model ID from the database

        :return: The model ID for the Criadex LLM

        """

        response = await self._criadex.manage.about(
            group_name=self.group_name("DOCUMENT")
        )
        return response

    async def update_group_content(
        self,
        index_type,
        file
    ):
        """
        Update a documents currently in the index

        :param file: The file
        :param index_type: The type of index the content resides in
        :return: The response from Criadex SDK with the given file name

        """

        return await self.add_group_content(
            file=file,
            is_update=True,
            index_type=index_type
        )

    async def add_group_content(
        self,
        index_type,
        file,
        is_update: bool = False
    ):
        """
        Upload a documents to the index

        :param index_type: The index to upload the file to
        :param file: The Criadex upload file schemata
        :param is_update: Whether it's an update or a new file
        :return: The response from Criadex SDK with your provided file name

        """

        response = await self._upload_group_file(
            index_type,
            file.model_dump(),
            is_update=is_update
        )
        return response

    async def delete_group_file(self, index_type, document_name):
        """
        Delete an item from an index

        :param index_type: The type of index
        :param document_name: The name of the documents

        """

        group_name = self.group_name(index_type=index_type)
        response = await self._criadex.content.delete(
            group_name=group_name,
            document_name=document_name
        )
        return response

    async def list_group_files(self, index_type):
        """
        List the content for a given index

        :param index_type: The type of index
        :return: The list of files

        """

        response = await self._criadex.content.list(
            group_name=self.group_name(index_type=index_type)
        )
        return response

    async def _upload_group_file(
            self,
            index_type: str,
            file: dict,
            is_update: bool
    ):
        """
        Upload a file to the index

        :param index_type: The index type
        :param file: The file upload
        :return: The response from the API

        """

        # Normalize node types in the document payload to the enum values
        # so the API won't reject common simple values like "text".
        file = self._normalize_document_payload(file)

        group_name = self.group_name(index_type=index_type)
        group_operation = self._criadex.content.update if is_update else self._criadex.content.upload

        # Always run the Criadex SDK call in a thread to avoid blocking the event loop.
        # This assumes the Criadex SDK methods are synchronous.
        response = await group_operation(group_name, file)
        return response

    def _normalize_document_payload(self, file: dict) -> dict:
        """
        Ensure every node in file['file_contents']['nodes'] has a valid
        `type` value expected by Criadex. Map common simple types to the
        Criadex enum and fill missing/unknown types with
        'UncategorizedText'. This is a defensive, backward-compatible
        normalization to avoid hard failures on upload.
        """
        # mapping from common short names to Criadex enum values
        type_map = {
            "text": "NarrativeText",
            "txt": "NarrativeText",
            "image": "Image",
            "figure": "FigureCaption",
            "title": "Title",
            "list": "ListItem",
            "table": "Table",
        }

        try:
            contents = file.get("file_contents") or {}
            nodes = contents.get("nodes") or []
        except Exception:
            return file

        for node in nodes:
            # ensure metadata exists
            if "metadata" not in node or node["metadata"] is None:
                node["metadata"] = {}

            # normalize type
            t = node.get("type")
            if isinstance(t, str) and t in type_map:
                node["type"] = type_map[t]
            elif isinstance(t, str):
                # leave known full enum values unchanged; unknown short
                # strings map to UncategorizedText to be accepted by Criadex
                if not t or t.lower() in ("", "none"):
                    node["type"] = "UncategorizedText"
                else:
                    # preserve if already looks like an enum (capitalized)
                    if t[0].isupper():
                        node["type"] = t
                    else:
                        node["type"] = "UncategorizedText"
            else:
                # missing or non-string -> fallback
                node["type"] = "UncategorizedText"

        # write back
        if contents is not None:
            contents["nodes"] = nodes
            file["file_contents"] = contents

        return file
