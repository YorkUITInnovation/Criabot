from __future__ import annotations

import re
from typing import List, Optional

from .schemas import CourseActivity, GradebookProposal


class ContentMapper:
    """
    Deterministic activity-to-category mapper used before plugin-side review.
    """

    MODULE_CATEGORY_HINTS = {
        "assign": "Assignments",
        "assignment": "Assignments",
        "quiz": "Quizzes",
        "lab": "Labs",
        "forum": "Participation",
        "attendance": "Participation",
    }

    NAME_CATEGORY_HINTS = (
        ("lab", "Labs"),
        ("quiz", "Quizzes"),
        ("midterm", "Midterm"),
        ("final", "Final Exam"),
        ("exam", "Final Exam"),
        ("project", "Projects"),
        ("participation", "Participation"),
        ("discussion", "Participation"),
    )

    def build_mapping(
        self,
        course_activities: List[CourseActivity],
        proposal: Optional[GradebookProposal],
    ) -> dict:
        proposal_categories = proposal.categories if proposal else []
        mapping = []
        unmatched = []

        for activity in course_activities:
            category_name, confidence, reasoning = self._suggest_category(activity, proposal_categories)
            item = {
                "moodle_cmid": activity.cmid,
                "module_type": activity.module,
                "activity_name": activity.name,
                "suggested_category": category_name,
                "confidence": confidence,
                "reasoning": reasoning,
            }
            if category_name is None:
                unmatched.append(item)
            else:
                mapping.append(item)

        return {
            "graded_activities": mapping,
            "unmatched_activities": unmatched,
            "resource_suggestions": [],
        }

    def _suggest_category(self, activity: CourseActivity, proposal_categories: list) -> tuple[Optional[str], float, str]:
        normalized_name = self._normalize(activity.name)
        category_names = [category.name for category in proposal_categories]

        for category in proposal_categories:
            normalized_category = self._normalize(category.name)
            if activity.name in category.items:
                return category.name, 0.95, "Mapped from the accepted gradebook proposal item list."
            if normalized_category and normalized_category in normalized_name:
                return category.name, 0.85, "Mapped by matching the activity name to the accepted category name."

        module_hint = self.MODULE_CATEGORY_HINTS.get((activity.module or "").lower())
        if module_hint:
            matched_category = self._resolve_existing_category(module_hint, category_names)
            return matched_category, 0.8, "Mapped from the Moodle activity module type."

        for token, hinted_category in self.NAME_CATEGORY_HINTS:
            if token in normalized_name:
                matched_category = self._resolve_existing_category(hinted_category, category_names)
                return matched_category, 0.75, "Mapped from activity name keywords."

        if proposal_categories:
            return proposal_categories[0].name, 0.55, "Mapped to the primary proposal category as a deterministic fallback."

        return None, 0.0, "No deterministic category match found."

    @staticmethod
    def _resolve_existing_category(category_name: str, existing_categories: List[str]) -> str:
        normalized_target = ContentMapper._normalize(category_name)
        for existing in existing_categories:
            if ContentMapper._normalize(existing) == normalized_target:
                return existing
        return category_name

    @staticmethod
    def _normalize(value: Optional[str]) -> str:
        if not value:
            return ""
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
