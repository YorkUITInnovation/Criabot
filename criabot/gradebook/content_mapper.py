from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

from .schemas import CourseActivity, GradebookProposal


logger = logging.getLogger(__name__)


class ContentMapper:
    """
    Deterministic activity-to-category mapper used before plugin-side review.
    """

    NOT_GRADED = "__not_graded__"

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

    def __init__(self, criadex=None, llm_model_id: Optional[str] = None) -> None:
        self._criadex = criadex
        self._llm_model_id = llm_model_id or "gpt-3.5-turbo"

    async def build_mapping(
        self,
        course_activities: List[CourseActivity],
        proposal: Optional[GradebookProposal],
    ) -> dict:
        proposal_categories = proposal.categories if proposal else []
        category_names = [c.name for c in proposal_categories]
        mapping = []
        unmatched = []

        # Stage 1: Try deterministic matching for all activities
        deterministic_results = {}
        llm_candidates = []  # Track activities needing LLM (low confidence or unmatched)

        for activity in course_activities:
            category_name, confidence, reasoning = self._suggest_category(activity, proposal_categories)
            deterministic_results[activity.cmid] = {
                "category": category_name,
                "confidence": confidence,
                "reasoning": reasoning,
                "from_deterministic": True,
            }
            # Stage 2: Only pass to LLM if deterministic confidence is too low
            if category_name and confidence < 0.8:
                llm_candidates.append(activity)
            elif not category_name:
                llm_candidates.append(activity)

        # Stage 2: Call LLM only for low-confidence or unmatched items (cost control)
        llm_by_cmid = {}
        if llm_candidates:
            llm_by_cmid = await self._llm_assignments(llm_candidates, category_names)

        # Combine results: prefer high-confidence deterministic, use LLM for low-confidence
        for activity in course_activities:
            deterministic = deterministic_results[activity.cmid]
            category_name = deterministic["category"]
            confidence = deterministic["confidence"]
            reasoning = deterministic["reasoning"]
            from_deterministic = True

            # Override with LLM result if available and deterministic was low confidence
            llm_pick = llm_by_cmid.get(activity.cmid)
            if llm_pick and confidence < 0.8:
                category_name = llm_pick["category"]
                confidence = float(llm_pick.get("confidence", 0.75))
                reasoning = llm_pick.get("reasoning") or "Mapped using LLM category classification."
                from_deterministic = False

            item = {
                "moodle_cmid": activity.cmid,
                "module_type": activity.module,
                "activity_name": activity.name,
                "suggested_category": category_name,
                "confirmed_category": category_name,
                "finalized": False,
                "confidence": confidence,
                "reasoning": reasoning,
                "mapping_method": "deterministic" if from_deterministic else "llm",
            }
            if category_name is None:
                unmatched.append(item)

            mapping.append(item)

        return {
            "graded_activities": mapping,
            "unmatched_activities": unmatched,
            "resource_suggestions": [],
        }

    async def _llm_assignments(self, course_activities: List[CourseActivity], category_names: List[str]) -> dict:
        if self._criadex is None or not category_names or not course_activities:
            return {}

        prompt = self._build_llm_prompt(course_activities, category_names)
        try:
            response = await self._criadex.agents.azure.chat(
                model_id=self._llm_model_id,
                agent_config={
                    "history": [
                        {
                            "role": "system",
                            "blocks": [{"type": "text", "text": "You classify Moodle activities into gradebook categories."}],
                            "additional_kwargs": {},
                            "metadata": {},
                        },
                        {
                            "role": "user",
                            "blocks": [{"type": "text", "text": prompt}],
                            "additional_kwargs": {},
                            "metadata": {},
                        },
                    ]
                },
            )
        except Exception:
            logger.exception("LLM mapping request failed; falling back to deterministic mapping.")
            return {}

        content = self._extract_chat_content(response)
        parsed = self._extract_json_array(content)
        if not parsed:
            return {}

        valid_categories = {name.lower(): name for name in category_names}
        valid_categories[self.NOT_GRADED] = self.NOT_GRADED

        out = {}
        for row in parsed:
            try:
                cmid = row.get("moodle_cmid")
                if cmid is None:
                    continue
                category = str(row.get("category") or "").strip()
                canonical = valid_categories.get(category.lower())
                if not canonical:
                    continue
                out[cmid] = {
                    "category": canonical,
                    "confidence": float(row.get("confidence", 0.75)),
                    "reasoning": str(row.get("reasoning") or "Mapped using LLM category classification."),
                }
            except Exception:
                continue

        return out

    def _build_llm_prompt(self, course_activities: List[CourseActivity], category_names: List[str]) -> str:
        activity_rows = []
        for activity in course_activities:
            activity_rows.append({
                "moodle_cmid": activity.cmid,
                "module": activity.module,
                "activity_name": activity.name,
            })

        # Build detailed category descriptions to help LLM distinguish
        category_descriptions = []
        for cat in category_names:
            hint = ""
            cat_lower = cat.lower()
            if "assignment" in cat_lower:
                hint = "Individual or group homework submissions"
            elif "quiz" in cat_lower:
                hint = "Quick assessments, short tests"
            elif "exam" in cat_lower or "midterm" in cat_lower or "final" in cat_lower:
                hint = "Major exams or comprehensive assessments"
            elif "lab" in cat_lower:
                hint = "Practical lab work, hands-on practice"
            elif "project" in cat_lower:
                hint = "Large creative or research projects"
            elif "participation" in cat_lower or "discussion" in cat_lower:
                hint = "Forum posts, discussions, engagement"
            
            if hint:
                category_descriptions.append(f"- {cat}: {hint}")
            else:
                category_descriptions.append(f"- {cat}")

        payload = {
            "allowed_categories": category_names,
            "category_descriptions": "\n".join(category_descriptions),
            "not_graded": self.NOT_GRADED,
            "activities": activity_rows,
            "instructions": [
                "You are a gradebook categorization expert.",
                "Carefully analyze each activity name and module type, then assign it to the MOST SPECIFIC category.",
                "Match activities precisely: 'quiz' activities → Quizzes, 'assign' → Assignments, exams → Exam categories.",
                "Use " + self.NOT_GRADED + " only for administrative items (announcements, links, resources) that should not count toward grades.",
                "Assign each activity to exactly ONE category from the allowed list or __not_graded__.",
                "Return ONLY a JSON array with objects: {moodle_cmid (int), category (string), confidence (0-1), reasoning (string)}.",
                "confidence: how sure you are (0.5-1.0). reasoning: brief explanation of the assignment.",
            ],
        }
        return json.dumps(payload, ensure_ascii=True)

    @staticmethod
    def _extract_chat_content(response) -> str:
        if isinstance(response, dict):
            return str(
                response.get("agent_response", {})
                .get("chat_response", {})
                .get("message", {})
                .get("content", "")
            )

        try:
            return str(response.verify().agent_response.chat_response.message.content)
        except Exception:
            return ""

    @staticmethod
    def _extract_json_array(text: str) -> list:
        if not text:
            return []
        cleaned = text.strip()
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            pass

        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            parsed = json.loads(cleaned[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

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
