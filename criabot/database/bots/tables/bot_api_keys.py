from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Integer,
    TIMESTAMP,
    String,
    Boolean,
    ForeignKey,
    func,
    insert,
    select,
    update,
    ChunkedIteratorResult,
    CursorResult,
)
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class BotApiKeyConfig(BaseModel):
    bot_id: int
    api_key: str
    revoked: bool = False


class BotApiKeyModel(BotApiKeyConfig):
    id: int
    created: datetime


class BotApiKeysTable(BaseTable):
    __tablename__ = "BotApiKeys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("Bots.id", ondelete="CASCADE"),
        nullable=False,
    )
    api_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now()
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BotApiKeysAPI(TableAPI):
    Schema = BotApiKeysTable

    async def insert(self, config: BotApiKeyConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema).values(**config.model_dump())
            )
            return result.lastrowid

    async def get_active_by_bot(self, bot_id: int) -> Optional[BotApiKeyModel]:
        """
        Retrieve the most recently created, non-revoked API key for a bot.
        """
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(
                    (self.Schema.bot_id == bot_id)
                    & (self.Schema.revoked.is_(False))
                )
                .order_by(self.Schema.created.desc())
            )
            entry: BotApiKeysTable = self.fetchone_or_none(result)

        return self.to_model(entry, BotApiKeyModel)

    async def revoke(self, api_key: str) -> None:
        """
        Mark a key as revoked.
        """
        async with self.get_async_session() as session:
            await session.execute(
                update(self.Schema)
                .values(revoked=True)
                .where(self.Schema.api_key == api_key)
            )

