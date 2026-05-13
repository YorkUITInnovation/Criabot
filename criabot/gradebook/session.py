from __future__ import annotations

import os
import re
import time
import uuid
from typing import Dict, List, TYPE_CHECKING

from .conversation import ConversationManager
from .content_mapper import ContentMapper
from .proposal import ProposalGenerator
from .schemas import CourseActivity, GradebookSessionRecord, MoodleResource
from criabot.cache.objects.gradebooks import _parse_time_to_seconds

if TYPE_CHECKING:
    from criabot.database.gradebook.gradebook_db import GradebookDatabaseAPI


class GradebookSessionEngine:
    def __init__(
        self,
        gradebook_db: 'GradebookDatabaseAPI' = None,
        gradebook_cache=None,
        criadex=None,
        mapping_llm_model_id: str | None = None,
    ) -> None:
        self._gradebook_db = gradebook_db
        self._gradebook_cache = gradebook_cache
        self._proposal_generator = ProposalGenerator()
        self._content_mapper = ContentMapper(criadex=criadex, llm_model_id=mapping_llm_model_id)
        self._conversation = ConversationManager()
        self._session_expire_seconds = _parse_time_to_seconds(
            os.environ.get("GRADEBOOK_SESSION_EXPIRE_TIME", "4h")
        )
        # Keep in-memory cache for active sessions
        self._active_sessions: Dict[str, GradebookSessionRecord] = {}
        self._proposal_history: Dict[str, List[dict]] = {}
        self._proposal_history_index: Dict[str, int] = {}

    @staticmethod
    def _history_key() -> str:
        return "_proposal_history"

    @staticmethod
    def _history_index_key() -> str:
        return "_proposal_history_index"

    def _load_history_from_session(self, session: GradebookSessionRecord) -> None:
        """Hydrate in-memory history from persisted session extraction when available."""
        extraction = session.extraction or {}
        raw_history = extraction.get(self._history_key())
        raw_index = extraction.get(self._history_index_key())
        if not isinstance(raw_history, list):
            return

        cleaned_history = [entry for entry in raw_history if isinstance(entry, dict)]
        if not cleaned_history:
            return

        if isinstance(raw_index, int):
            idx = max(0, min(raw_index, len(cleaned_history) - 1))
        else:
            idx = len(cleaned_history) - 1

        self._proposal_history[session.session_id] = cleaned_history
        self._proposal_history_index[session.session_id] = idx

    def _persist_history_to_session(self, session: GradebookSessionRecord) -> None:
        """Persist in-memory history into extraction so undo/redo survives process restarts."""
        session.extraction = session.extraction or {}
        session.extraction[self._history_key()] = list(self._proposal_history.get(session.session_id, []))
        session.extraction[self._history_index_key()] = int(self._proposal_history_index.get(session.session_id, -1))

    def _ensure_history_initialized(self, session: GradebookSessionRecord) -> None:
        """Ensure history exists for current session proposal before mutating or restoring."""
        history = self._proposal_history.get(session.session_id)
        if history:
            return

        self._load_history_from_session(session)
        history = self._proposal_history.get(session.session_id)
        if history:
            return

        if session.proposal is not None:
            self._proposal_history[session.session_id] = [session.proposal.model_dump()]
            self._proposal_history_index[session.session_id] = 0
            self._persist_history_to_session(session)

    def _push_proposal_history(self, session: GradebookSessionRecord) -> None:
        if session.proposal is None:
            return

        proposal_dict = session.proposal.model_dump()
        history = self._proposal_history.setdefault(session.session_id, [])
        current_index = self._proposal_history_index.get(session.session_id, -1)

        if current_index >= 0 and history[current_index] == proposal_dict:
            return

        if current_index < len(history) - 1:
            del history[current_index + 1:]

        history.append(proposal_dict)
        self._proposal_history_index[session.session_id] = len(history) - 1
        self._persist_history_to_session(session)

    def _is_undo_prompt(self, prompt: str) -> bool:
        lowered = prompt.lower()
        return bool(re.search(r"\bundo\b", lowered))

    def _is_redo_prompt(self, prompt: str) -> bool:
        lowered = prompt.lower()
        return bool(re.search(r"\bredo\b", lowered))

    def _restore_from_history(self, session: GradebookSessionRecord, direction: str) -> bool:
        self._ensure_history_initialized(session)
        history = self._proposal_history.get(session.session_id, [])
        if not history:
            return False

        index = self._proposal_history_index.get(session.session_id, len(history) - 1)
        if direction == "undo":
            next_index = index - 1
        else:
            next_index = index + 1

        if next_index < 0 or next_index >= len(history):
            return False

        from .schemas import GradebookProposal

        session.proposal = GradebookProposal.parse_obj(history[next_index])
        self._proposal_history_index[session.session_id] = next_index
        self._persist_history_to_session(session)
        return True

    @staticmethod
    def _has_syllabus(resources: List[MoodleResource]) -> bool:
        for resource in resources:
            name = (resource.name or "").lower()
            section = (resource.section or "").lower()
            preview = (resource.content_preview or "").lower()
            if (
                "syllabus" in name
                or "syllabi" in name
                or "syllabe" in name
                or "plan de cours" in name
                or "plan du cours" in name
                or "programme" in name
                or "outline" in name
                or "grading" in preview
                or "assessment" in preview
                or "syllabus" in preview
                or "syllabe" in preview
                or "plan de cours" in preview
                or "plan du cours" in preview
                or "bar\u00e8me" in preview
                or section in {"0", "section 0"}
                or "%" in preview
            ):
                return True
        return False

    @staticmethod
    def _detect_syllabus_sources(resources: List[MoodleResource]) -> List[str]:
        sources: List[str] = []
        for resource in resources:
            name = (resource.name or "").lower()
            section = (resource.section or "").lower()
            preview = (resource.content_preview or "").lower()
            if (
                "syllabus" in name
                or "syllabi" in name
                or "syllabe" in name
                or "plan de cours" in name
                or "plan du cours" in name
                or "programme" in name
                or "outline" in name
                or "grading" in preview
                or "assessment" in preview
                or "syllabus" in preview
                or "syllabe" in preview
                or "bar\u00e8me" in preview
                or "plan de cours" in preview
                or "plan du cours" in preview
                or section in {"0", "section 0"}
                or "%" in preview
            ):
                if resource.name:
                    sources.append(resource.name)
        return sources

    async def _save_session(self, session: GradebookSessionRecord) -> None:
        session.last_touched_at = int(time.time())
        if self._gradebook_db:
            from criabot.database.gradebook.tables.gradebook_sessions import GradebookSessionsConfig

            config = GradebookSessionsConfig(
                session_id=session.session_id,
                course_id=session.course_id,
                professor_id=session.professor_id,
                bot_name=session.bot_name,
                phase=session.phase,
                moodle_resources_json=[resource.model_dump() for resource in session.moodle_resources],
                course_activities_json=[activity.model_dump() for activity in session.course_activities],
                proposal_json=session.proposal.model_dump() if session.proposal else None,
                extraction_json=session.extraction,
                metadata_json=session.content_mapping,
            )

            if await self._gradebook_db.sessions.exists(session.session_id):
                await self._gradebook_db.sessions.update_session(
                    session_id=session.session_id,
                    updates=config.model_dump(exclude={"session_id", "course_id", "professor_id", "bot_name"})
                )
            else:
                await self._gradebook_db.sessions.insert(config)

        if self._gradebook_cache:
            from criabot.cache.objects.gradebooks import GradebookSessionModel
            await self._gradebook_cache.set(
                session.session_id,
                GradebookSessionModel(
                    session_id=session.session_id,
                    course_id=session.course_id,
                    professor_id=session.professor_id,
                    bot_name=session.bot_name,
                    phase=session.phase,
                    moodle_resources=[resource.model_dump() for resource in session.moodle_resources],
                    course_activities=[activity.model_dump() for activity in session.course_activities],
                    proposal=session.proposal.model_dump() if session.proposal else None,
                    extraction=session.extraction,
                    content_mapping=session.content_mapping,
                    last_touched_at=session.last_touched_at,
                )
            )

    async def _load_session_from_db(self, session_id: str) -> GradebookSessionRecord | None:
        if not self._gradebook_db:
            return None
        session_model = await self._gradebook_db.sessions.retrieve(session_id)
        if not session_model:
            return None

        from .schemas import GradebookProposal
        from .schemas import MoodleResource, CourseActivity

        moodle_resources = [MoodleResource(**resource) for resource in (session_model.moodle_resources_json or [])]
        course_activities = [CourseActivity(**activity) for activity in (session_model.course_activities_json or [])]
        proposal = None
        if session_model.proposal_json is not None:
            from .schemas import GradebookProposal
            proposal = GradebookProposal.parse_obj(session_model.proposal_json)

        record = GradebookSessionRecord(
            session_id=session_model.session_id,
            course_id=session_model.course_id,
            professor_id=session_model.professor_id,
            bot_name=session_model.bot_name,
            phase=session_model.phase,
            moodle_resources=moodle_resources,
            course_activities=course_activities,
            extraction=session_model.extraction_json,
            proposal=proposal,
            content_mapping=session_model.metadata_json,
            last_touched_at=int(session_model.updated_at.timestamp()),
        )
        self._active_sessions[session_id] = record
        if self._gradebook_cache:
            from criabot.cache.objects.gradebooks import GradebookSessionModel
            await self._gradebook_cache.set(
                session_id,
                GradebookSessionModel(**record.model_dump())
            )
        return record

    async def _load_session_from_cache(self, session_id: str) -> GradebookSessionRecord | None:
        if not self._gradebook_cache:
            return None
        session_model = await self._gradebook_cache.get(session_id)
        if not session_model:
            return None

        moodle_resources = [MoodleResource(**resource) for resource in (session_model.moodle_resources or [])]
        course_activities = [CourseActivity(**activity) for activity in (session_model.course_activities or [])]
        proposal = None
        if session_model.proposal is not None:
            from .schemas import GradebookProposal
            proposal = GradebookProposal.parse_obj(session_model.proposal)

        record = GradebookSessionRecord(
            session_id=session_model.session_id,
            course_id=session_model.course_id,
            professor_id=session_model.professor_id,
            bot_name=session_model.bot_name,
            phase=session_model.phase,
            moodle_resources=moodle_resources,
            course_activities=course_activities,
            extraction=session_model.extraction,
            proposal=proposal,
            content_mapping=session_model.content_mapping,
            last_touched_at=session_model.last_touched_at,
        )
        self._active_sessions[session_id] = record
        return record

    def _is_expired(self, session: GradebookSessionRecord | None) -> bool:
        if session is None or session.phase == "COMPLETED":
            return False
        if session.last_touched_at is None:
            return False
        return int(time.time()) - int(session.last_touched_at) > self._session_expire_seconds

    async def start(
        self,
        course_id: str,
        professor_id: str,
        bot_name: str,
        moodle_resources: List[MoodleResource],
        course_activities: List[CourseActivity],
    ) -> GradebookSessionRecord:
        if not course_activities and moodle_resources:
            # Fallback: derive activity-like records from visible Moodle resources.
            course_activities = [
                CourseActivity(
                    cmid=resource.cmid,
                    module=resource.type,
                    name=resource.name,
                )
                for resource in moodle_resources
                if resource.name
            ]
        session_id = "gb-" + str(uuid.uuid4())
        syllabus_sources = self._detect_syllabus_sources(moodle_resources)
        has_syllabus = len(syllabus_sources) > 0
        phase = "ANALYSIS" if has_syllabus else "INTAKE"
        proposal = self._proposal_generator.generate_initial(course_activities) if phase == "ANALYSIS" else None
        record = GradebookSessionRecord(
            session_id=session_id,
            course_id=course_id,
            professor_id=professor_id,
            bot_name=bot_name,
            phase=phase,
            moodle_resources=moodle_resources,
            course_activities=course_activities,
            extraction={
                "has_syllabus": has_syllabus,
                "syllabus_sources": syllabus_sources,
            },
            proposal=proposal,
            last_touched_at=int(time.time()),
        )
        self._active_sessions[session_id] = record
        self._push_proposal_history(record)
        await self._save_session(record)
        return record

    async def get(self, session_id: str) -> GradebookSessionRecord | None:
        if session_id in self._active_sessions:
            session = self._active_sessions[session_id]
            if self._is_expired(session):
                self._active_sessions.pop(session_id, None)
                if self._gradebook_cache:
                    await self._gradebook_cache.delete(session_id)
                return None
            return session
        session = await self._load_session_from_cache(session_id)
        if session:
            if self._is_expired(session):
                self._active_sessions.pop(session_id, None)
                if self._gradebook_cache:
                    await self._gradebook_cache.delete(session_id)
                return None
            return session
        session = await self._load_session_from_db(session_id)
        if self._is_expired(session):
            self._active_sessions.pop(session_id, None)
            if self._gradebook_cache:
                await self._gradebook_cache.delete(session_id)
            return None
        return session

    async def chat(self, session_id: str, prompt: str) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        session.extraction = session.extraction or {}
        session.extraction["latest_prompt"] = prompt

        self._ensure_history_initialized(session)

        if self._is_undo_prompt(prompt):
            restored = self._restore_from_history(session, direction="undo")
            session.phase = "REFINEMENT" if session.proposal else session.phase
            await self._save_session(session)
            return session

        if self._is_redo_prompt(prompt):
            restored = self._restore_from_history(session, direction="redo")
            session.phase = "REFINEMENT" if session.proposal else session.phase
            await self._save_session(session)
            return session

        session.phase = self._conversation.next_phase(session, prompt)
        if session.phase in {"ANALYSIS", "REFINEMENT", "PROPOSAL"} and session.proposal is None:
            session.proposal = self._proposal_generator.generate_initial(session.course_activities)
            self._push_proposal_history(session)

        if session.proposal is not None:
            # Capture baseline before applying prompt so undo can restore prior state.
            self._push_proposal_history(session)
            session.proposal = self._proposal_generator.update_from_prompt(session.proposal, prompt)
            self._push_proposal_history(session)

        await self._save_session(session)
        return session

    async def accept(self, session_id: str) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        if session.phase in {"ACCEPTED", "COMPLETED"} and session.content_mapping is not None:
            return session

        session.phase = "ACCEPTED"
        session.content_mapping = await self._content_mapper.build_mapping(
            course_activities=session.course_activities,
            proposal=session.proposal,
        )

        await self._save_session(session)

        if self._gradebook_db:
            from criabot.database.gradebook.tables.gradebook_results import GradebookResultsConfig
            session_db = await self._gradebook_db.sessions.retrieve(session.session_id)
            if session_db:
                existing_result = await self._gradebook_db.results.retrieve_by_session(session_db.id)
                if existing_result is None:
                    result_config = GradebookResultsConfig(
                        session_id=session_db.id,
                        course_id=session.course_id,
                        professor_id=session.professor_id,
                        gradebook_json=session.proposal.model_dump() if session.proposal else {},
                        content_mapping_json=session.content_mapping
                    )
                    await self._gradebook_db.results.insert(result_config)
                else:
                    await self._gradebook_db.results.update_by_session(
                        session_id=session_db.id,
                        updates={
                            "gradebook_json": session.proposal.model_dump() if session.proposal else {},
                            "content_mapping_json": session.content_mapping,
                        }
                    )

        return session

    async def reset(self, session_id: str, keep_extraction: bool = True) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        has_syllabus = bool((session.extraction or {}).get("has_syllabus"))
        session.proposal = self._proposal_generator.generate_initial(session.course_activities)
        session.content_mapping = None
        if not keep_extraction:
            session.extraction = {"has_syllabus": has_syllabus, "syllabus_sources": list((session.extraction or {}).get("syllabus_sources") or [])}

        session.phase = "PROPOSAL" if has_syllabus else "INTAKE"
        self._proposal_history[session.session_id] = []
        self._proposal_history_index[session.session_id] = -1
        self._push_proposal_history(session)
        self._persist_history_to_session(session)
        await self._save_session(session)

        # Clear any persisted finalized result for this session so reset is clean.
        if self._gradebook_db:
            session_db = await self._gradebook_db.sessions.retrieve(session.session_id)
            if session_db:
                await self._gradebook_db.results.delete_by_session(session_db.id)

        return session

    async def delete(self, session_id: str) -> dict:
        """Delete a gradebook session from memory, cache, and database.
        
        Returns:
            dict with keys:
            - 'success': bool indicating if deletion occurred
            - 'existed': bool indicating if session/data was found
            - 'message': str with status details
        """
        session = self._active_sessions.pop(session_id, None)
        existed = session is not None

        # Cache cleanup
        if self._gradebook_cache:
            await self._gradebook_cache.delete(session_id)

        # In-memory history cleanup
        self._proposal_history.pop(session_id, None)
        self._proposal_history_index.pop(session_id, None)

        # Database cleanup
        deleted_db = False
        if self._gradebook_db:
            session_db = await self._gradebook_db.sessions.retrieve(session_id)
            if session_db:
                existed = True
                await self._gradebook_db.results.delete_by_session(session_db.id)
            deleted_db = await self._gradebook_db.sessions.delete_session(session_id)

        success = deleted_db or session is not None
        
        return {
            'success': success,
            'existed': existed,
            'message': f"Session {session_id} deleted." if success else f"Session {session_id} not found."
        }


    async def finalize(
        self,
        session_id: str,
        confirmed_mapping: List[dict],
    ) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        if session.content_mapping is None:
            session = await self.accept(session_id)

        graded_activities = list((session.content_mapping or {}).get("graded_activities", []))
        activity_by_cmid = {
            item.get("moodle_cmid"): item
            for item in graded_activities
            if item.get("moodle_cmid") is not None
        }

        for confirmed in confirmed_mapping:
            existing = activity_by_cmid.get(confirmed.get("moodle_cmid"))
            if existing is None:
                continue
            existing["suggested_category"] = confirmed.get("category")
            existing["confirmed_category"] = confirmed.get("category")
            existing["finalized"] = True

        session.phase = "COMPLETED"
        await self._save_session(session)

        if self._gradebook_db:
            from criabot.database.gradebook.tables.gradebook_results import GradebookResultsConfig

            session_db = await self._gradebook_db.sessions.retrieve(session.session_id)
            if session_db:
                existing_result = await self._gradebook_db.results.retrieve_by_session(session_db.id)
                if existing_result is None:
                    await self._gradebook_db.results.insert(
                        GradebookResultsConfig(
                            session_id=session_db.id,
                            course_id=session.course_id,
                            professor_id=session.professor_id,
                            gradebook_json=session.proposal.model_dump() if session.proposal else {},
                            content_mapping_json=session.content_mapping,
                        )
                    )
                    existing_result = await self._gradebook_db.results.retrieve_by_session(session_db.id)
                else:
                    await self._gradebook_db.results.update_by_session(
                        session_id=session_db.id,
                        updates={"content_mapping_json": session.content_mapping},
                    )

                if existing_result and not existing_result.pushed_to_moodle:
                    await self._gradebook_db.results.mark_pushed_to_moodle(existing_result.id)

        return session
