from abc import abstractmethod
from contextlib import asynccontextmanager
from typing import TypeVar, Type, AsyncGenerator, Optional

from pydantic import BaseModel
from sqlalchemy import Row, Connection, ChunkedIteratorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncAttrs, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


class BaseTable(AsyncAttrs, DeclarativeBase):
    pass


T = TypeVar("T", bound=BaseTable)
M = TypeVar("M", bound=BaseModel)


class TableAPI:
    """Generic MySQL table supporting operations"""

    Schema: Type[T] = NotImplemented

    def __init__(self, engine: AsyncEngine):
        """
        Instantiate the table

        :param pool: SQL Pool

        """

        self._engine: AsyncEngine = engine
        self._async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def to_model(cls, t: Optional[T], m: Type[M]) -> Optional[M]:
        if t is None:
            return None

        data: dict = {}
        for field in m.__fields__.keys():
            data[field] = t.__dict__[field]

        return m(**data)

    @classmethod
    def fetchone_or_none(cls, result: ChunkedIteratorResult) -> Optional[T]:
        result: Optional[Row] = result.fetchone()
        return result.tuple()[0] if result else None

    async def initialize(self) -> None:
        def create_all(sync_conn: Connection):
            self.Schema.metadata.create_all(sync_conn, checkfirst=True)

        async with self._engine.begin() as connection:
            await connection.run_sync(create_all)

    @asynccontextmanager
    async def get_async_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a session from the engine"""
        async with self._async_session_maker() as session:
            async with session.begin():
                yield session

    @abstractmethod
    async def insert(self, **kwargs) -> None:
        """Insert a row into the database"""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, **kwargs) -> None:
        """Delete a row from the database"""
        raise NotImplementedError

    @abstractmethod
    async def retrieve(self, **kwargs) -> tuple:
        """Retrieve a row of the table from the database"""
        raise NotImplementedError

    @abstractmethod
    async def exists(self, **kwargs) -> bool:
        """See if a row exists in the database"""
        raise NotImplementedError


class BaseDatabaseAPI:
    """
    API for interfacing with the database

    """

    def __init__(self, engine: AsyncEngine):
        """
        Instantiate the database API

        :param engine: SQLAlchemy Engine

        """

        self._engine: AsyncEngine = engine

    @abstractmethod
    async def initialize(self) -> None:
        """
        Instantiate the database

        :return: None

        """

        raise NotImplementedError

    @property
    def engine(self) -> AsyncEngine:
        """
        Retrieve the engine

        :return: Engine instance

        """

        return self._engine
