from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import Integer, TIMESTAMP, String, func, insert, delete, select, ChunkedIteratorResult, CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class BotsConfig(BaseModel):
    name: str


class BotsModel(BotsConfig):
    id: int
    created: datetime


class BotsTable(BaseTable):
    __tablename__ = "Bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


class BotsAPI(TableAPI):
    Schema = BotsTable

    async def insert(self, config: BotsConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema)
                .values(**config.model_dump())
            )

            return result.lastrowid

    async def delete(self, name: str) -> None:
        async with self.get_async_session() as session:
            await session.execute(
                delete(self.Schema)
                .where(self.Schema.name == name)
            )

    async def retrieve(self, name: str) -> Optional[BotsModel]:
        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .where(self.Schema.name == name)
            )

            entry: BotsTable = self.fetchone_or_none(result)
        return self.to_model(entry, BotsModel)

    async def retrieve_by_id(self, bot_id: int) -> Optional[BotsModel]:
        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .where(self.Schema.id == bot_id)
            )

            entry: BotsTable = self.fetchone_or_none(result)
        return self.to_model(entry, BotsModel)

    async def retrieve_by_ids(self, bot_ids: List[int]) -> List[BotsModel]:
        """Batch retrieve bots by IDs to avoid N+1 queries"""
        if not bot_ids:
            return []
        
        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .where(self.Schema.id.in_(bot_ids))
            )
            entries: List[BotsTable] = result.scalars().all()
        
        return [self.to_model(entry, BotsModel) for entry in entries]

    async def retrieve_id(self, name: str) -> Optional[int]:
        model: Optional[BotsModel] = await self.retrieve(name=name)
        return model.id if model else None

    async def exists(self, *names: str) -> bool:
        """
        Check if all specified bots exist.
        Returns True only if ALL provided names exist.
        """
        if not names:
            return False

        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .filter(self.Schema.name.in_(names))
            )
            entries: List[BotsTable] = result.scalars().all()

        found_names = {entry.name for entry in entries}
        return len(found_names) == len(names)

    async def any_exists(self, *names: str) -> bool:
        """
        Check if any of the specified bots exist.
        """
        if not names:
            return False

        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .filter(self.Schema.name.in_(names))
            )
            entry: Optional[BotsTable] = self.fetchone_or_none(result)

        return entry is not None


