from __future__ import annotations

import uuid
from typing import Dict, List, TYPE_CHECKING

from .conversation import ConversationManager
from .proposal import ProposalGenerator
from .schemas import CourseActivity, GradebookSessionRecord, MoodleResource

if TYPE_CHECKING:
    from criabot.database.gradebook.gradebook_db import GradebookDatabaseAPI


class GradebookSessionEngine:
    def __init__(self, gradebook_db: 'GradebookDatabaseAPI' = None, gradebook_cache=None) -> None:
        self._gradebook_db = gradebook_db
        self._gradebook_cache = gradebook_cache
        self._proposal_generator = ProposalGenerator()
        self._conversation = ConversationManager()
        # Keep in-memory cache for active sessions
        self._active_sessions: Dict[str, GradebookSessionRecord] = {}

    @staticmethod
    def _has_syllabus(resources: List[MoodleResource]) -> bool:
        for resource in resources:
            name = (resource.name or "").lower()
            preview = (resource.content_preview or "").lower()
            if "syllabus" in name or "grading" in preview or "%" in preview:
                return True
        return False

    async def _save_session(self, session: GradebookSessionRecord) -> None:
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
        )
        self._active_sessions[session_id] = record
        return record

    async def start(
        self,
        course_id: str,
        professor_id: str,
        bot_name: str,
        moodle_resources: List[MoodleResource],
        course_activities: List[CourseActivity],
    ) -> GradebookSessionRecord:
        session_id = "gb-" + str(uuid.uuid4())
        phase = "ANALYSIS" if self._has_syllabus(moodle_resources) else "INTAKE"
        proposal = self._proposal_generator.generate_initial(course_activities) if phase == "ANALYSIS" else None
        record = GradebookSessionRecord(
            session_id=session_id,
            course_id=course_id,
            professor_id=professor_id,
            bot_name=bot_name,
            phase=phase,
            moodle_resources=moodle_resources,
            course_activities=course_activities,
            proposal=proposal,
        )
        self._active_sessions[session_id] = record
        await self._save_session(record)
        return record

    async def get(self, session_id: str) -> GradebookSessionRecord | None:
        if session_id in self._active_sessions:
            return self._active_sessions[session_id]
        session = await self._load_session_from_cache(session_id)
        if session:
            return session
        return await self._load_session_from_db(session_id)

    async def chat(self, session_id: str, prompt: str) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        session.phase = self._conversation.next_phase(session, prompt)
        if session.phase in {"ANALYSIS", "REFINEMENT", "PROPOSAL"} and session.proposal is None:
            session.proposal = self._proposal_generator.generate_initial(session.course_activities)

        if session.proposal is not None:
            session.proposal = self._proposal_generator.update_from_prompt(session.proposal, prompt)

        await self._save_session(session)
        return session

    async def accept(self, session_id: str) -> GradebookSessionRecord:
        session = self._active_sessions.get(session_id) or await self.get(session_id)
        if session is None:
            raise KeyError("gradebook session not found")

        session.phase = "ACCEPTED"
        session.content_mapping = {
            "graded_activities": [
                {
                    "moodle_cmid": activity.cmid,
                    "module_type": activity.module,
                    "activity_name": activity.name,
                    "suggested_category": "Assignments",
                    "confidence": 0.7,
                    "reasoning": "Baseline mapping; refine during plugin-side review.",
                }
                for activity in session.course_activities
            ],
            "unmatched_activities": [],
            "resource_suggestions": [],
        }

        await self._save_session(session)

        if self._gradebook_db:
            from criabot.database.gradebook.tables.gradebook_results import GradebookResultsConfig
            session_db = await self._gradebook_db.sessions.retrieve(session.session_id)
            if session_db:
                result_config = GradebookResultsConfig(
                    session_id=session_db.id,
                    course_id=session.course_id,
                    professor_id=session.professor_id,
                    gradebook_json=session.proposal.model_dump() if session.proposal else {},
                    content_mapping_json=session.content_mapping
                )
                await self._gradebook_db.results.insert(result_config)

        return session
