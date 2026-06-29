from abc import abstractmethod
from contextlib import asynccontextmanager
from typing import TypeVar

from redis import asyncio as aioredis
from redis.asyncio import ConnectionPool, Redis
from pydantic import BaseModel

T = TypeVar('T', bound=BaseModel)


class CacheObject:
    """Generic Redis object model supporting operations"""

    #: Optional namespace prepended to every key (e.g. ``"chat:"``). Keeping
    #: each cache object in its own namespace prevents key collisions between
    #: chats, gradebook sessions, web-search results, and any other tenants
    #: (e.g. SearXNG) that may share the same Redis instance.
    key_prefix: str = ""

    def __init__(self, pool: ConnectionPool):
        """
        Instantiate the table

        :param pool: SQL Pool

        """

        self._pool: ConnectionPool = pool

    def _key(self, key: str) -> str:
        """Apply the cache object's namespace to a raw key."""
        return f"{self.key_prefix}{key}"

    @asynccontextmanager
    async def redis(self) -> Redis:
        """
        Context manager for retrieving a client bound to the shared pool.

        Exiting the ``async with`` releases the connection back to the pool;
        no explicit ``close()`` is needed (and the old explicit call used the
        deprecated API).

        """

        async with aioredis.Redis(connection_pool=self._pool) as redis:
            yield redis

    @abstractmethod
    async def set(self, key: str, val: T, **kwargs) -> None:
        """Insert an object into the cache"""
        raise NotImplementedError

    @abstractmethod
    async def get(self, key: str, **kwargs) -> T:
        """Retrieve an object from the cache"""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key: str, **kwargs) -> None:
        """Delete an object from the cache"""
        raise NotImplementedError

    @abstractmethod
    async def exists(self, key: str, **kwargs) -> bool:
        """Check if an object exists in the database"""
        raise NotImplementedError


class BaseCacheAPI:
    """
    API for interfacing with the database

    """

    def __init__(self, pool: ConnectionPool):
        """
        Instantiate the database API

        :param pool: SQL Pool

        """

        self._pool: ConnectionPool = pool

    @property
    def pool(self) -> ConnectionPool:
        """
        Retrieve the pool

        :return: Pool instance

        """

        return self._pool
