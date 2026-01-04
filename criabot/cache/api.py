from redis.asyncio import ConnectionPool

from criabot.cache.core import BaseCacheAPI
from criabot.cache.objects.chats import Chats


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
