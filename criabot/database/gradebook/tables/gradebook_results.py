from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel
from sqlalchemy import Integer, TIMESTAMP, String, JSON, Boolean, func, insert, update, select, delete, ChunkedIteratorResult, CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class GradebookResultsConfig(BaseModel):
    session_id: int
    course_id: str
    professor_id: str
    gradebook_json: Dict[str, Any]
    content_mapping_json: Optional[Dict[str, Any]] = None
    pushed_to_moodle: bool = False
    moodle_sync_at: Optional[datetime] = None


class GradebookResultsModel(GradebookResultsConfig):
    id: int
    created_at: datetime


class GradebookResultsTable(BaseTable):
    __tablename__ = "GradebookResults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[str] = mapped_column(String(128), nullable=False)
    professor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gradebook_json: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_mapping_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    pushed_to_moodle: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    moodle_sync_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


class GradebookResultsAPI(TableAPI):
    Schema = GradebookResultsTable

    async def insert(self, config: GradebookResultsConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema)
                .values(**config.model_dump())
            )
            await session.commit()
            return result.lastrowid

    async def update_result(self, result_id: int, updates: Dict[str, Any]) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                update(self.Schema)
                .where(self.Schema.id == result_id)
                .values(**updates)
            )
            await session.commit()
            return result.rowcount > 0

    async def update_by_session(self, session_id: int, updates: Dict[str, Any]) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                update(self.Schema)
                .where(self.Schema.session_id == session_id)
                .values(**updates)
            )
            await session.commit()
            return result.rowcount > 0

    async def retrieve(self, result_id: int) -> Optional[GradebookResultsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.id == result_id)
            )
            entry: GradebookResultsTable = self.fetchone_or_none(result)
        return self.to_model(entry, GradebookResultsModel)

    async def retrieve_by_session(self, session_id: int) -> Optional[GradebookResultsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.session_id == session_id)
            )
            entry: GradebookResultsTable = self.fetchone_or_none(result)
        return self.to_model(entry, GradebookResultsModel)

    async def retrieve_by_course(self, course_id: str) -> List[GradebookResultsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.course_id == course_id)
                .order_by(self.Schema.created_at.desc())
            )
            entries: List[GradebookResultsTable] = result.scalars().all()
        return [self.to_model(entry, GradebookResultsModel) for entry in entries]

    async def retrieve_by_professor(self, professor_id: str) -> List[GradebookResultsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.professor_id == professor_id)
                .order_by(self.Schema.created_at.desc())
            )
            entries: List[GradebookResultsTable] = result.scalars().all()
        return [self.to_model(entry, GradebookResultsModel) for entry in entries]

    async def mark_pushed_to_moodle(self, result_id: int) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                update(self.Schema)
                .where(self.Schema.id == result_id)
                .values(
                    pushed_to_moodle=True,
                    moodle_sync_at=func.now()
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def delete_by_session(self, session_id: int) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                delete(self.Schema)
                .where(self.Schema.session_id == session_id)
            )
            await session.commit()
            return result.rowcount > 0