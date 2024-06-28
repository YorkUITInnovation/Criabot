from abc import abstractmethod
from contextlib import asynccontextmanager
from typing import TypeVar

import aioredis
from aioredis import ConnectionPool, Redis
from pydantic import BaseModel

T = TypeVar('T', bound=BaseModel)


class CacheObject:
    """Generic Redis object model supporting operations"""

    def __init__(self, pool: ConnectionPool):
        """
        Instantiate the table

        :param pool: SQL Pool

        """

        self._pool: ConnectionPool = pool

    @asynccontextmanager
    async def redis(self) -> Redis:
        """
        Context manager for retrieving the cursor from the pool
        :return: Cursor instance

        """

        async with aioredis.Redis(connection_pool=self._pool) as redis:
            try:
                yield redis
            finally:
                await redis.close()

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
