from typing import Optional, List
from pydantic import BaseModel
from sqlalchemy import Integer, ForeignKey, UniqueConstraint, insert, delete, select, \
    ChunkedIteratorResult, CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class BotParentsConfig(BaseModel):
    child_bot_id: int
    parent_bot_id: int
    priority: int = 0


class BotParentsModel(BotParentsConfig):
    id: int


class BotParentsTable(BaseTable):
    __tablename__ = "BotParents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    child_bot_id: Mapped[int] = mapped_column(
        ForeignKey("Bots.id", ondelete="CASCADE"),
        nullable=False
    )
    parent_bot_id: Mapped[int] = mapped_column(
        ForeignKey("Bots.id", ondelete="CASCADE"),
        nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint('child_bot_id', 'parent_bot_id', name='uq_bot_parents'),
    )


class BotParentsAPI(TableAPI):
    Schema = BotParentsTable

    async def insert(self, config: BotParentsConfig) -> int:
        """Create a parent-child relationship"""
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema)
                .values(**config.model_dump())
            )
            return result.lastrowid

    async def delete(self, child_bot_id: int, parent_bot_id: int) -> None:
        """Remove a parent-child relationship"""
        async with self.get_async_session() as session:
            await session.execute(
                delete(self.Schema)
                .where(
                    (self.Schema.child_bot_id == child_bot_id) &
                    (self.Schema.parent_bot_id == parent_bot_id)
                )
            )

    async def delete_all_by_child(self, child_bot_id: int) -> None:
        """Remove all parent relationships for a child bot"""
        async with self.get_async_session() as session:
            await session.execute(
                delete(self.Schema)
                .where(self.Schema.child_bot_id == child_bot_id)
            )

    async def delete_all_by_parent(self, parent_bot_id: int) -> None:
        """Remove all child relationships for a parent bot"""
        async with self.get_async_session() as session:
            await session.execute(
                delete(self.Schema)
                .where(self.Schema.parent_bot_id == parent_bot_id)
            )

    async def get_by_child(self, child_bot_id: int) -> List[BotParentsModel]:
        """Get all parent relationships for a child bot, ordered by priority"""
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.child_bot_id == child_bot_id)
                .order_by(self.Schema.priority.asc())
            )
            entries = result.scalars().all()
            return [self.to_model(entry, BotParentsModel) for entry in entries]

    async def get_by_parent(self, parent_bot_id: int) -> List[BotParentsModel]:
        """Get all child relationships for a parent bot"""
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.parent_bot_id == parent_bot_id)
            )
            entries = result.scalars().all()
            return [self.to_model(entry, BotParentsModel) for entry in entries]

    async def exists(self, child_bot_id: int, parent_bot_id: int) -> bool:
        """Check if a parent-child relationship exists"""
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(
                    (self.Schema.child_bot_id == child_bot_id) &
                    (self.Schema.parent_bot_id == parent_bot_id)
                )
            )
            entry = self.fetchone_or_none(result)
            return entry is not None

    async def get_parent_ids(self, child_bot_id: int) -> List[int]:
        """Get all parent bot IDs for a child bot, ordered by priority"""
        relationships = await self.get_by_child(child_bot_id)
        return [rel.parent_bot_id for rel in relationships]

    async def get_child_ids(self, parent_bot_id: int) -> List[int]:
        """Get all child bot IDs for a parent bot"""
        relationships = await self.get_by_parent(parent_bot_id)
        return [rel.child_bot_id for rel in relationships]

