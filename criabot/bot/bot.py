import time
import uuid
from typing import Dict, Awaitable, Callable

from CriadexSDK import CriadexSDK
from CriadexSDK.routers.content import GroupContentDeleteRoute, GroupContentListRoute, GroupContentUploadRoute
from CriadexSDK.routers.content.search import SearchGroupConfig, GroupContentSearchRoute
from CriadexSDK.routers.content.upload import ContentUploadConfig
from CriadexSDK.routers.groups import GroupAboutRoute
from CriadexSDK.routers.groups.about import GroupInfo
from CriadexSDK.routers.groups.create import IndexTypes

from criabot.bot.schemas import GroupContentResponse
from criabot.cache.api import BotCacheAPI
from criabot.cache.objects.chats import ChatModel


class Bot:
    # This must NOT be changed lol
    INDEX_SUFFIX: Dict[IndexTypes, str] = {
        "QUESTION": "-question-index",
        "DOCUMENT": "-document-index",
        # "CACHE": "-cache-index"
    }

    def __init__(
            self,
            name: str,
            criadex: CriadexSDK,
            bot_cache: BotCacheAPI
    ):
        self._name: str = name
        self._criadex: CriadexSDK = criadex
        self._cache_api: BotCacheAPI = bot_cache

    @property
    def criadex(self) -> CriadexSDK:
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

    def group_name(self, index_type: IndexTypes) -> str:
        """
        Get the index name from the index type

        :param index_type: Type of index
        :return: Its name

        """

        return self.bot_group_name(self._name, index_type)

    @classmethod
    def bot_group_name(cls, bot_name: str, index_type: IndexTypes) -> str:
        """
        Get the name of a given index for a bot

        """

        return bot_name + cls.INDEX_SUFFIX[index_type]

    async def search_group(
            self,
            index_type: IndexTypes,
            search_config: SearchGroupConfig
    ) -> GroupContentSearchRoute.Response:
        """
        Ask a documents on one of the Bot's indexes

        :param index_type: The type of index to query
        :param search_config: The config for searching the index via Criadex
        :return: Vector DB Response

        """

        result: GroupContentSearchRoute.Response = await self._criadex.content.search(
            group_name=self.group_name(index_type),
            search_config=search_config
        )

        return result.verify()

    async def retrieve_group_info(self) -> GroupInfo:
        """
        Retrieve the LLM model ID from the database

        :return: The model ID for the Criadex LLM

        """

        response: GroupAboutRoute.Response = await self._criadex.manage.about(
            group_name=self.group_name("DOCUMENT")
        )

        response.verify()
        return response.info

    async def update_group_content(
            self,
            index_type: IndexTypes,
            file: ContentUploadConfig
    ) -> GroupContentResponse:
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
            index_type: IndexTypes,
            file: ContentUploadConfig,
            is_update: bool = False
    ) -> GroupContentResponse:
        """
        Upload a documents to the index

        :param index_type: The index to upload the file to
        :param file: The Criadex upload file schemata
        :param is_update: Whether it's an update or a new file
        :return: The response from Criadex SDK with your provided file name

        """

        response: GroupContentUploadRoute.Response = await self._upload_group_file(
            index_type,
            file,
            is_update=is_update
        )

        return GroupContentResponse(
            response=response,
            document_name=file.file_name
        )

    async def delete_group_file(self, index_type: IndexTypes, document_name: str) -> GroupContentDeleteRoute.Response:
        """
        Delete an item from an index

        :param index_type: The type of index
        :param document_name: The name of the documents

        """

        group_name: str = self.group_name(index_type=index_type)

        response: GroupContentDeleteRoute.Response = await self._criadex.content.delete(
            group_name=group_name,
            document_name=document_name
        )

        return response.verify()

    async def list_group_files(self, index_type: IndexTypes) -> GroupContentListRoute.Response:
        """
        List the content for a given index

        :param index_type: The type of index
        :return: The list of files

        """

        response: GroupContentListRoute.Response = await self._criadex.content.list(
            group_name=self.group_name(index_type=index_type)
        )

        return response.verify()

    async def _upload_group_file(
            self,
            index_type: IndexTypes,
            file: ContentUploadConfig,
            is_update: bool
    ) -> GroupContentUploadRoute.Response:
        """
        Upload a file to the index

        :param index_type: The index type
        :param file: The file upload
        :return: The response from the API

        """

        # Get the associated name
        group_name: str = self.group_name(index_type=index_type)

        # Update the index
        group_operation: Callable[[group_name, ContentUploadConfig], Awaitable] = (
            self._criadex.content.update.execute if is_update else self._criadex.content.upload.execute
        )

        # Upload the file
        response: GroupContentUploadRoute.Response = await group_operation(
            group_name, file
        )

        # Verify
        return response.verify()
