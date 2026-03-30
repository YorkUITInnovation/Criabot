from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import Integer, TIMESTAMP, ForeignKey, func, insert, select, ChunkedIteratorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class BotModelConfig(BaseModel):
    bot_id: int
    llm_model_id: int
    rerank_model_id: int


class BotModelModel(BotModelConfig):
    id: int
    created: datetime


class BotModelsTable(BaseTable):
    __tablename__ = "BotModels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("Bots.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    llm_model_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rerank_model_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


class BotModelsAPI(TableAPI):
    Schema = BotModelsTable

    async def insert(self, config: BotModelConfig) -> int:
        async with self.get_async_session() as session:
            result = await session.execute(
                insert(self.Schema).values(**config.model_dump())
            )
            return result.lastrowid

    async def retrieve_by_bot_id(self, bot_id: int) -> Optional[BotModelModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema).where(self.Schema.bot_id == bot_id)
            )
            entry: Optional[BotModelsTable] = self.fetchone_or_none(result)

        return self.to_model(entry, BotModelModel)

