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

    async def retrieve_id(self, name: str) -> Optional[int]:
        model: Optional[BotsModel] = await self.retrieve(name=name)
        return model.id if model else None

    async def exists(self, *names: str) -> bool:
        async with self.get_async_session() as session:

            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .filter(self.Schema.name.in_(names))
            )

            entry: BotsTable = self.fetchone_or_none(result)

        return bool(entry)


