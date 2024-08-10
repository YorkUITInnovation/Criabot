from typing import Optional

from pydantic import BaseModel
from sqlalchemy import Integer, Numeric, Boolean, Text, ForeignKey, insert, delete, select, update, \
    CursorResult, ChunkedIteratorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class BotParametersTable(BaseTable):
    __tablename__ = "BotParameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("Bots.id"))

    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_reply_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[float] = mapped_column(Numeric(2, 1), nullable=False)
    top_p: Mapped[float] = mapped_column(Numeric(2, 1), nullable=False)

    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    min_k: Mapped[float] = mapped_column(Numeric(2, 1), nullable=False)

    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    min_n: Mapped[float] = mapped_column(Numeric(2, 1), nullable=False)

    llm_generate_related_prompts: Mapped[bool] = mapped_column(Boolean, nullable=False)

    no_context_message: Mapped[str] = mapped_column(Text, nullable=False)
    no_context_use_message: Mapped[bool] = mapped_column(Boolean, nullable=False)
    no_context_llm_guess: Mapped[bool] = mapped_column(Boolean, nullable=False)
    system_message: Mapped[str] = mapped_column(Text, nullable=False)


class BotParametersBaseConfig(BaseModel):
    # Model Params

    max_input_tokens: int = 2000  # Max context in chats
    max_reply_tokens: int = 1024  # Max REPLY TOKENS
    temperature: float = 0.9  # Max REPLY temperature
    top_p: float = 0  # Max REPLY P

    # Retrieval Params
    top_k: int = 10
    min_k: float = 0.5

    # Rerank Params
    top_n: int = 3
    min_n: float = 0.7

    # Context Params
    llm_generate_related_prompts: bool = True

    no_context_message: str = "Sorry, I'm not sure about that."  # No context reply message
    no_context_use_message: bool = False
    no_context_llm_guess: bool = False
    system_message: Optional[str] = None  # System message to embed


class BotParametersConfig(BotParametersBaseConfig):
    # Ref
    bot_id: int


class BotParametersModel(BotParametersConfig):
    id: int


class BotParametersAPI(TableAPI):
    Schema = BotParametersTable

    async def insert(self, config: BotParametersConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema)
                .values(**config.model_dump())
            )

            return result.lastrowid

    async def update(self, bot_id: int, config: BotParametersBaseConfig) -> None:
        async with self.get_async_session() as session:
            await session.execute(
                update(self.Schema)
                .values(**config.model_dump())
                .where(self.Schema.bot_id == bot_id)
            )

    async def delete(self, bot_id: int) -> None:
        async with self.get_async_session() as session:
            await session.execute(
                delete(self.Schema)
                .where(self.Schema.bot_id == bot_id)
            )

    async def retrieve(self, bot_id: int) -> BotParametersModel:
        async with self.get_async_session() as session:
            result: Optional[ChunkedIteratorResult] = await session.execute(
                select(self.Schema)
                .where(self.Schema.bot_id == bot_id)
            )

            entry: BotParametersTable = self.fetchone_or_none(result)
        return self.to_model(entry, BotParametersModel)

    async def exists(self, bot_id: int) -> bool:
        return bool(await self.retrieve(bot_id=bot_id))
