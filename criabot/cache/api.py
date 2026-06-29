from redis.asyncio import ConnectionPool

from criabot.cache.core import BaseCacheAPI
from criabot.cache.objects.chats import Chats
from criabot.cache.objects.gradebooks import Gradebooks
from criabot.cache.objects.web_searches import WebSearches
from criabot.cache.objects.reranks import Reranks
from criabot.cache.objects.faq_searches import FaqSearches


class BotCacheAPI(BaseCacheAPI):
    """
    API for interfacing with the index in the database

    """

    def __init__(self, pool: ConnectionPool):
        """
        Instantiate the index database API
        :param pool: SQL Pool

        """

        super().__init__(pool)

        self.chats: Chats = Chats(pool)
        self.gradebooks: Gradebooks = Gradebooks(pool)
        self.web_searches: WebSearches = WebSearches(pool)
        self.reranks: Reranks = Reranks(pool)
        self.faq_searches: FaqSearches = FaqSearches(pool)
