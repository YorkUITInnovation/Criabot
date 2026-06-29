from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel
from sqlalchemy import Integer, TIMESTAMP, String, JSON, Enum, func, insert, update, select, delete, ChunkedIteratorResult, CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from criabot.database.table import TableAPI, BaseTable


class GradebookSessionsConfig(BaseModel):
    session_id: str
    course_id: str
    professor_id: str
    bot_name: str
    phase: str = "INTAKE"
    moodle_resources_json: Optional[List[Dict[str, Any]]] = None
    course_activities_json: Optional[List[Dict[str, Any]]] = None
    proposal_json: Optional[Dict[str, Any]] = None
    extraction_json: Optional[Dict[str, Any]] = None
    metadata_json: Optional[Dict[str, Any]] = None


class GradebookSessionsModel(GradebookSessionsConfig):
    id: int
    created_at: datetime
    updated_at: datetime


class GradebookSessionsTable(BaseTable):
    __tablename__ = "GradebookSessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    course_id: Mapped[str] = mapped_column(String(128), nullable=False)
    professor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_name: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(
        Enum('INTAKE', 'ANALYSIS', 'BASELINE_READY', 'PROPOSAL', 'REFINEMENT', 'ACCEPTED', 'CATEGORIZING', 'COMPLETED', 'FAILED'),
        nullable=False,
        default='INTAKE'
    )
    moodle_resources_json: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    course_activities_json: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    proposal_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    extraction_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())


class GradebookSessionsAPI(TableAPI):
    Schema = GradebookSessionsTable

    async def insert(self, config: GradebookSessionsConfig) -> int:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                insert(self.Schema)
                .values(**config.model_dump())
            )
            await session.commit()
            return result.lastrowid

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                update(self.Schema)
                .where(self.Schema.session_id == session_id)
                .values(**updates)
            )
            await session.commit()
            return result.rowcount > 0

    async def retrieve(self, session_id: str) -> Optional[GradebookSessionsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.session_id == session_id)
            )
            entry: GradebookSessionsTable = self.fetchone_or_none(result)
        return self.to_model(entry, GradebookSessionsModel)

    async def retrieve_by_course(self, course_id: str) -> List[GradebookSessionsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.course_id == course_id)
                .order_by(self.Schema.created_at.desc())
            )
            entries: List[GradebookSessionsTable] = result.scalars().all()
        return [self.to_model(entry, GradebookSessionsModel) for entry in entries]

    async def retrieve_by_professor(self, professor_id: str) -> List[GradebookSessionsModel]:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema)
                .where(self.Schema.professor_id == professor_id)
                .order_by(self.Schema.created_at.desc())
            )
            entries: List[GradebookSessionsTable] = result.scalars().all()
        return [self.to_model(entry, GradebookSessionsModel) for entry in entries]

    async def delete_session(self, session_id: str) -> bool:
        async with self.get_async_session() as session:
            result: CursorResult = await session.execute(
                delete(self.Schema)
                .where(self.Schema.session_id == session_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def exists(self, session_id: str) -> bool:
        async with self.get_async_session() as session:
            result: ChunkedIteratorResult = await session.execute(
                select(self.Schema.id)
                .where(self.Schema.session_id == session_id)
            )
            entry = result.fetchone()
        return entry is not None