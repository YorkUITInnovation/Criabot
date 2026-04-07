from __future__ import annotations

import uuid
from typing import Dict, List

from .conversation import ConversationManager
from .proposal import ProposalGenerator
from .schemas import CourseActivity, GradebookSessionRecord, MoodleResource


class GradebookSessionEngine:
    def __init__(self) -> None:
        self._sessions: Dict[str, GradebookSessionRecord] = {}
        self._proposal_generator = ProposalGenerator()
        self._conversation = ConversationManager()

    @staticmethod
    def _has_syllabus(resources: List[MoodleResource]) -> bool:
        for resource in resources:
            name = (resource.name or "").lower()
            preview = (resource.content_preview or "").lower()
            if "syllabus" in name or "grading" in preview or "%" in preview:
                return True
        return False

    def start(
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
        self._sessions[session_id] = record
        return record

    def get(self, session_id: str) -> GradebookSessionRecord | None:
        return self._sessions.get(session_id)

    def chat(self, session_id: str, prompt: str) -> GradebookSessionRecord:
        session = self._sessions[session_id]
        session.phase = self._conversation.next_phase(session, prompt)
        if session.phase in {"ANALYSIS", "REFINEMENT", "PROPOSAL"} and session.proposal is None:
            session.proposal = self._proposal_generator.generate_initial(session.course_activities)
        return session

    def accept(self, session_id: str) -> GradebookSessionRecord:
        session = self._sessions[session_id]
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
        return session
