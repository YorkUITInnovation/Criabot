from datetime import datetime
from typing import Optional, Dict, Any, List

from pydantic import BaseModel
from sqlalchemy import Integer, TIMESTAMP, String, JSON, Boolean, Float, func, insert, select, ChunkedIteratorResult, CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class FAQSyncLogConfig(BaseModel):
    run_at: datetime
    completed_at: Optional[datetime] = None
    source_url: str
    group_name: str
    max_pages: int
    timeout_seconds: float
    trigger_graph_build: bool = True
    state: str
    pages_crawled: int = 0
    indexed_files: int = 0
    duplicate_files: int = 0
    error: Optional[str] = None
    graph_build_job: Optional[Dict[str, Any]] = None


class FAQSyncLogModel(FAQSyncLogConfig):
    id: int
    created_at: datetime


class FAQSyncLogsTable(BaseTable):
    __tablename__ = "FAQSyncLogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    trigger_graph_build: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    pages_crawled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    graph_build_job: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


class FAQSyncLogsAPI(TableAPI):
    Schema = FAQSyncLogsTable

    async def insert(self, config: FAQSyncLogConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema).values(**config.model_dump())
            )
            await session.commit()
            return result.lastrowid

    async def delete(self, log_id: int) -> None:
        raise NotImplementedError("FAQ sync logs are append-only.")

    async def retrieve(self, log_id: int) -> Optional[FAQSyncLogModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema).where(self.Schema.id == log_id)
            )
            entry: FAQSyncLogsTable = self.fetchone_or_none(result)
        return self.to_model(entry, FAQSyncLogModel)

    async def retrieve_latest(self, limit: int = 10) -> List[FAQSyncLogModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .order_by(self.Schema.run_at.desc(), self.Schema.id.desc())
                .limit(limit)
            )
            entries: List[FAQSyncLogsTable] = result.scalars().all()
        return [self.to_model(entry, FAQSyncLogModel) for entry in entries]

    async def exists(self, log_id: int) -> bool:
        return bool(await self.retrieve(log_id=log_id))
