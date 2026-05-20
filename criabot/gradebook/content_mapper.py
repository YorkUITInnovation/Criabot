from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import List, Optional

from .schemas import CourseActivity, GradebookProposal


logger = logging.getLogger(__name__)


class ContentMapper:
    """
    Deterministic activity-to-category mapper used before plugin-side review.
    Implements YorkU eClass and Moodle standard mapping with proper distinction
    between Activity Items (course modules) and Manual Grade Items (gradebook-only).
    """

    NOT_GRADED = "__not_graded__"
    UNCATEGORIZED = "__uncategorized__"

    # Module type to category mapping (Moodle activity types)
    MODULE_CATEGORY_HINTS = {
        "assign": "Assignments",
        "quiz": "Quizzes",
        "lab": "Labs",
        "forum": "Participation",
        "attendance": "Participation",
    }

    # Keyword to category mapping with priority ordering
    # Each tuple: (keyword, category_target, min_confidence)
    # Keywords are checked case-insensitively with word boundaries
    # Longer/more specific keywords come first to avoid partial matches
    NAME_CATEGORY_KEYWORDS = [
        # Longer/more specific keywords first
        ("knowledge check", "Quizzes", 0.80),
        ("final exam", "Final Exam", 0.95),
        ("mid-term", "Midterm", 0.95),
        ("final project", "Projects", 0.90),
        # Single-word keywords
        ("homework", "Assignments", 0.85),
        ("submission", "Assignments", 0.85),
        ("assignment", "Assignments", 0.90),
        ("laboratory", "Labs", 0.85),
        ("practical", "Labs", 0.80),
        ("midterm", "Midterm", 0.95),
        ("project", "Projects", 0.85),
        ("participation", "Participation", 0.90),
        ("discussion", "Participation", 0.85),
        ("lab", "Labs", 0.90),
        ("quiz", "Quizzes", 0.90),
        ("test", "Quizzes", 0.85),
        ("assessment", "Quizzes", 0.70),
        ("forum", "Participation", 0.80),
        ("final", "Final Exam", 0.90),
        ("exam", "Quizzes", 0.75),
        ("hw", "Assignments", 0.80),
        ("summative", "Final Exam", 0.75),
    ]

    def __init__(self, criadex=None, llm_model_id: Optional[str] = None) -> None:
        self._criadex = criadex
        self._llm_model_id = llm_model_id or "gpt-3.5-turbo"

    async def build_mapping(
        self,
        course_activities: List[CourseActivity],
        proposal: Optional[GradebookProposal],
        mapper_chat_id: Optional[str] = None,
    ) -> dict:
        proposal_categories = proposal.categories if proposal else []
        category_names = [c.name for c in proposal_categories]
        mapping = []
        unmatched = []
        uncategorized = []

        # Stage 1: Try deterministic matching for all activities
        deterministic_results = {}
        llm_candidates = []  # Track activities needing LLM (low confidence or unmatched)

        for idx, activity in enumerate(course_activities):
            activity_key = self._activity_key(activity, idx)
            category_name, confidence, reasoning, item_type = self._suggest_category(activity, proposal_categories)
            deterministic_results[activity_key] = {
                "category": category_name,
                "confidence": confidence,
                "reasoning": reasoning,
                "from_deterministic": True,
                "item_type": item_type,
            }
            # Stage 2: Only pass to LLM if deterministic confidence is too low and not explicitly uncategorized
            if category_name == self.UNCATEGORIZED:
                # Marked as uncategorized; let LLM try to suggest based on context
                llm_candidates.append(activity)
            elif category_name and confidence < 0.8:
                llm_candidates.append(activity)
            elif not category_name:
                llm_candidates.append(activity)

        # Stage 2: Call LLM only for low-confidence or unmatched items (cost control)
        llm_by_cmid = {}
        active_mapper_chat_id = mapper_chat_id
        if llm_candidates:
            llm_by_cmid, active_mapper_chat_id = await self._llm_assignments(
                llm_candidates,
                category_names,
                preferred_chat_id=mapper_chat_id,
                return_chat_id=True,
            )

        # Combine results: prefer high-confidence deterministic, use LLM for low-confidence
        for idx, activity in enumerate(course_activities):
            activity_key = self._activity_key(activity, idx)
            deterministic = deterministic_results[activity_key]
            category_name = deterministic["category"]
            confidence = deterministic["confidence"]
            reasoning = deterministic["reasoning"]
            from_deterministic = True
            item_type = deterministic["item_type"]

            # Override with LLM result if available and deterministic was low confidence or uncategorized
            llm_pick = llm_by_cmid.get(activity.cmid)
            if llm_pick and (confidence < 0.8 or category_name == self.UNCATEGORIZED):
                category_name = llm_pick["category"]
                confidence = float(llm_pick.get("confidence", 0.75))
                reasoning = llm_pick.get("reasoning") or "Mapped using LLM category classification with course context."
                from_deterministic = False

            item = {
                "moodle_cmid": activity.cmid,
                "grade_item_id": activity.grade_item_id,
                "module_type": activity.module,
                "itemtype": activity.itemtype or "mod",
                "activity_name": activity.name,
                "item_source": item_type,  # "activity" or "manual" grade item
                "suggested_category": category_name,
                "confirmed_category": category_name,
                "finalized": False,
                "confidence": confidence,
                "reasoning": reasoning,
                "mapping_method": "deterministic" if from_deterministic else "llm",
            }
            
            if category_name == self.UNCATEGORIZED:
                uncategorized.append(item)
            elif category_name is None:
                unmatched.append(item)

            mapping.append(item)

        validation_errors = self.validate_mapping(mapping, proposal)

        # Normalize invalid category references to uncategorized so downstream
        # handling does not apply stale/deleted categories.
        invalid_by_cmid = {
            item.get("moodle_cmid"): item
            for item in validation_errors
            if item.get("moodle_cmid") is not None
        }
        if invalid_by_cmid:
            for item in mapping:
                cmid = item.get("moodle_cmid")
                if cmid in invalid_by_cmid:
                    item["suggested_category"] = self.UNCATEGORIZED
                    item["confirmed_category"] = self.UNCATEGORIZED
                    if item not in uncategorized:
                        uncategorized.append(item)

        return {
            "graded_activities": mapping,
            "unmatched_activities": unmatched,
            "uncategorized_activities": uncategorized,
            "resource_suggestions": [],
            "validation_errors": validation_errors,
            "llm_mapper_chat_id": active_mapper_chat_id,
        }

    def validate_mapping(self, mapping_rows: List[dict], proposal: Optional[GradebookProposal]) -> List[dict]:
        """Validate mapped category labels exist in the current proposal."""
        if proposal is None:
            return []

        valid_categories = {
            str(category.name).strip().lower()
            for category in (proposal.categories or [])
            if str(category.name).strip()
        }
        issues: List[dict] = []
        for row in mapping_rows or []:
            suggested = str(row.get("suggested_category") or "").strip()
            if not suggested:
                continue
            if suggested in {self.UNCATEGORIZED, self.NOT_GRADED}:
                continue
            if suggested.lower() in valid_categories:
                continue
            issues.append(
                {
                    "moodle_cmid": row.get("moodle_cmid"),
                    "activity_name": row.get("activity_name"),
                    "missing_category": suggested,
                    "reason": "Category was removed, renamed, or not present in the current proposal.",
                }
            )
        return issues

    async def _llm_assignments(
        self,
        course_activities: List[CourseActivity],
        category_names: List[str],
        preferred_chat_id: Optional[str] = None,
        return_chat_id: bool = False,
    ) -> dict | tuple[dict, Optional[str]]:
        def _ret(data: dict, chat_id: Optional[str]) -> dict | tuple[dict, Optional[str]]:
            if return_chat_id:
                return data, chat_id
            return data

        if not course_activities:
            logger.warning("No course activities provided for LLM assignments.")
            return _ret({}, preferred_chat_id)
        if not category_names:
            logger.warning("No category names provided for LLM assignments.")
            return _ret({}, preferred_chat_id)
        if self._criadex is None:
            logger.error("Criadex instance is not initialized. Cannot perform LLM assignments.")
            return _ret({}, preferred_chat_id)

        prompt = self._build_llm_prompt(course_activities, category_names)
        response = None
        resolved_chat_id: Optional[str] = None
        # Keep chat_id on every attempt because Criadex contract requires it.
        for attempt in range(3):
            if attempt == 0 and preferred_chat_id:
                chat_id = preferred_chat_id
            else:
                chat_id = f"gradebook-mapper-{uuid.uuid4().hex[:12]}"
            try:
                ragflow_tenant_id = os.getenv("RAGFLOW_TENANT_ID")
                await self._criadex.agents.azure.ensure_dialog(
                    chat_id=chat_id,
                    model_id=self._llm_model_id,
                    tenant_id=ragflow_tenant_id,
                )
            except Exception as e:
                logger.warning(f"Could not ensure gradebook mapper dialog: {e}")

            agent_config = {
                "history": [
                    {
                        "role": "system",
                        "blocks": [
                            {
                                "type": "text",
                                "text": "You are a Moodle gradebook expert. Classify grade items into categories based on "
                                       "YorkU eClass and Moodle standards. For items without clear matches, you may suggest "
                                       f"'{self.UNCATEGORIZED}' if the category genuinely doesn't fit existing categories. "
                                       "Prioritize accuracy over defaults. Always explain your reasoning."
                            }
                        ],
                        "additional_kwargs": {},
                        "metadata": {},
                    },
                    {
                        "role": "user",
                        "blocks": [{"type": "text", "text": prompt}],
                        "additional_kwargs": {},
                        "metadata": {},
                    },
                ],
            }
            agent_config["chat_id"] = chat_id

            try:
                response = await self._criadex.agents.azure.chat(
                    model_id=self._llm_model_id,
                    agent_config=agent_config,
                )
                resolved_chat_id = chat_id
                break
            except Exception as e:
                err_text = str(e).lower()
                ownership_conflict = ("don't own the chat" in err_text or "do not own the chat" in err_text)
                if ownership_conflict and attempt < 2:
                    logger.warning(
                        "LLM mapper chat ownership conflict for chat_id %s. Retrying with a new chat_id.",
                        chat_id,
                    )
                    continue
                if ownership_conflict:
                    logger.warning(
                        "LLM mapper chat ownership conflict persisted after retries; falling back to deterministic mapping."
                    )
                else:
                    logger.exception(f"LLM mapping request failed: {e}")
                return _ret({}, preferred_chat_id)

        content = self._extract_chat_content(response)
        if not content:
            logger.error("LLM response content is empty. Falling back to deterministic mapping.")
            return _ret({}, preferred_chat_id)

        parsed = self._extract_json_array(content)
        if not parsed:
            logger.error("Failed to parse LLM response into JSON array. Falling back to deterministic mapping.")
            return _ret({}, preferred_chat_id)

        valid_categories = {name.lower(): name for name in category_names}
        valid_categories[self.NOT_GRADED] = self.NOT_GRADED
        valid_categories[self.UNCATEGORIZED] = self.UNCATEGORIZED

        out = {}
        for row in parsed:
            try:
                cmid = row.get("moodle_cmid")
                if cmid is None:
                    logger.warning("LLM response row missing 'moodle_cmid'. Skipping row.")
                    continue
                category = str(row.get("category") or "").strip()
                canonical = valid_categories.get(category.lower())
                if not canonical:
                    logger.warning(f"Invalid category '{category}' in LLM response. Skipping row.")
                    continue
                out[cmid] = {
                    "category": canonical,
                    "confidence": float(row.get("confidence", 0.75)),
                    "reasoning": str(row.get("reasoning") or "Mapped using LLM category classification."),
                }
            except Exception as e:
                logger.exception(f"Error processing LLM response row: {e}")
                continue

        return _ret(out, resolved_chat_id or preferred_chat_id)

    def _build_llm_prompt(self, course_activities: List[CourseActivity], category_names: List[str]) -> str:
        activity_rows = []
        for activity in course_activities:
            item_source = "Activity Module" if (activity.cmid and activity.module) else "Manual Grade Item"
            activity_rows.append({
                "moodle_cmid": activity.cmid,
                "module": activity.module or "N/A",
                "activity_name": activity.name,
                "item_source": item_source,
            })

        # Build detailed category descriptions to help LLM distinguish
        category_descriptions = []
        for cat in category_names:
            hint = ""
            cat_lower = cat.lower()
            if "assignment" in cat_lower:
                hint = "Individual or group homework submissions, written work, coding assignments"
            elif "quiz" in cat_lower:
                hint = "Quick assessments, knowledge checks, short tests, quizzes"
            elif "exam" in cat_lower or "midterm" in cat_lower or "final" in cat_lower:
                hint = "Major exams or comprehensive assessments (midterm, final, summative)"
            elif "lab" in cat_lower:
                hint = "Practical lab work, hands-on experiments, laboratory sessions"
            elif "project" in cat_lower:
                hint = "Large creative or research projects, capstone work"
            elif "participation" in cat_lower or "discussion" in cat_lower:
                hint = "Forum posts, discussions, engagement, classroom participation"
            
            if hint:
                category_descriptions.append(f"- **{cat}**: {hint}")
            else:
                category_descriptions.append(f"- **{cat}**: (no description provided)")

        # Build keyword reference section
        keyword_reference = """
                            **Keyword Mapping Reference (YorkU eClass Standard):**
                            - Assignment → Assignments (submissions, homework, projects, work)
                            - Quiz/Test/Knowledge Check → Quizzes
                            - Midterm/Mid-term/Exam 1 → Midterm
                            - Final/Final Exam/Summative → Final Exam
                            - Lab/Laboratory/Practical → Labs
                            - Project → Projects
                            - Participation/Discussion/Forum → Participation
                            """

        payload = {
            "allowed_categories": category_names,
            "category_descriptions": "\n".join(category_descriptions),
            "uncategorized": self.UNCATEGORIZED,
            "not_graded": self.NOT_GRADED,
            "activities": activity_rows,
            "keyword_reference": keyword_reference,
            "instructions": [
                "You are a Moodle gradebook expert following YorkU eClass standards.",
                "Analyze each grade item (activity or manual) and assign it to ONE category from the allowed list.",
                "Use the keyword reference above to guide your categorization.",
                "If an item name contains clear keywords (Quiz, Assignment, Midterm, etc.), prioritize that mapping.",
                "For Activity Modules (course activities with modules), check module type: quiz→Quizzes, assign→Assignments, etc.",
                f"If an item genuinely doesn't fit any category, use '{self.UNCATEGORIZED}' so professor can manually review.",
                f"Use '{self.NOT_GRADED}' ONLY for administrative items (announcements, resources, links) with zero grade points.",
                "DO NOT default everything to 'Assignments' — be precise and use keyword matching.",
                "Return ONLY a JSON array with objects: {moodle_cmid (int), category (string), confidence (0.5-1.0), reasoning (string)}.",
                "Reasoning must explain WHY you chose this category (e.g., 'Matched keyword Quiz in name' or 'Quiz module type').",
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

    def _suggest_category(self, activity: CourseActivity, proposal_categories: list) -> tuple[Optional[str], float, str, str]:
        """
        Suggest a category for an activity using deterministic rules.
        
        Returns: (category_name, confidence, reasoning, item_source)
                where item_source is "activity" or "manual"
        """
        normalized_name = self._normalize(activity.name)
        category_names = [category.name for category in proposal_categories]
        trust_proposal_items = self._should_trust_proposal_items(proposal_categories)

        # Determine item source: Activity (has cmid and module) or Manual grade item
        item_source = "activity" if (activity.cmid and activity.module) else "manual"

        module_candidate_name: Optional[str] = None
        module_candidate_confidence = 0.0
        module_candidate_reasoning: Optional[str] = None
        if item_source == "activity":
            module_hint = self.MODULE_CATEGORY_HINTS.get((activity.module or "").lower())
            if module_hint:
                matched_category = self._resolve_existing_category(module_hint, category_names)
                if matched_category:
                    module_candidate_name = matched_category
                    module_candidate_confidence = 0.8
                    module_candidate_reasoning = f"Mapped from activity module type '{activity.module}'."

        keyword_candidate_name: Optional[str] = None
        keyword_candidate_confidence = 0.0
        keyword_candidate_length = 0
        keyword_candidate_reasoning: Optional[str] = None
        for keyword, target_category, min_confidence in self.NAME_CATEGORY_KEYWORDS:
            normalized_keyword = self._normalize(keyword)
            if normalized_keyword and normalized_keyword in normalized_name:
                matched_category = self._resolve_existing_category(target_category, category_names)
                if matched_category and (
                    min_confidence > keyword_candidate_confidence
                    or (
                        min_confidence == keyword_candidate_confidence
                        and len(normalized_keyword) > keyword_candidate_length
                    )
                ):
                    keyword_candidate_name = matched_category
                    keyword_candidate_confidence = min_confidence
                    keyword_candidate_length = len(normalized_keyword)
                    keyword_candidate_reasoning = f"Matched keyword '{keyword}' in {item_source} name."

        # Priority 1: Check if item is explicitly in proposal categories
        for category in proposal_categories:
            normalized_category = self._normalize(category.name)
            if trust_proposal_items and activity.name in category.items:
                # Guard against noisy proposal item lists that place quiz/lab/project-like
                # activities under Assignments in multi-category proposals.
                deterministic_name = None
                deterministic_confidence = 0.0
                deterministic_reasoning = None
                if keyword_candidate_name and keyword_candidate_confidence >= module_candidate_confidence:
                    deterministic_name = keyword_candidate_name
                    deterministic_confidence = keyword_candidate_confidence
                    deterministic_reasoning = keyword_candidate_reasoning
                elif module_candidate_name:
                    deterministic_name = module_candidate_name
                    deterministic_confidence = module_candidate_confidence
                    deterministic_reasoning = module_candidate_reasoning

                if (
                    normalized_category in {"assignment", "assignments"}
                    and deterministic_name
                    and self._normalize(deterministic_name) not in {"assignment", "assignments"}
                    and deterministic_confidence >= 0.8
                ):
                    return (
                        deterministic_name,
                        deterministic_confidence,
                        deterministic_reasoning or f"Mapped by deterministic rule for {item_source}.",
                        item_source,
                    )
                return category.name, 0.95, f"Mapped from {item_source} in the accepted gradebook proposal item list.", item_source
            if normalized_category and normalized_category in normalized_name:
                return category.name, 0.85, f"Mapped by matching {item_source} name to category '{category.name}'.", item_source

        # Priority 2: Check module type hint (for activity items only)
        if module_candidate_name:
            return module_candidate_name, module_candidate_confidence, module_candidate_reasoning or "Mapped from activity module type.", item_source

        # Priority 3: Check name keywords with confidence scoring
        if keyword_candidate_name:
            return keyword_candidate_name, keyword_candidate_confidence, keyword_candidate_reasoning, item_source

        # Priority 4: No match found — return UNCATEGORIZED instead of default fallback
        # This signals to UI and LLM that this item needs explicit user review
        logger.warning(
            f"No deterministic match found for {item_source} '{activity.name}' (module: {activity.module}). "
            "Item flagged as UNCATEGORIZED for LLM suggestion and user review."
        )
        return self.UNCATEGORIZED, 0.0, f"No matching keywords found. {item_source} needs manual categorization or LLM suggestion.", item_source

    @staticmethod
    def _resolve_existing_category(category_name: str, existing_categories: List[str]) -> Optional[str]:
        normalized_target = ContentMapper._normalize(category_name)
        for existing in existing_categories:
            if ContentMapper._normalize(existing) == normalized_target:
                return existing
        return None

    @staticmethod
    def _normalize(value: Optional[str]) -> str:
        if not value:
            return ""
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    @staticmethod
    def _activity_key(activity: CourseActivity, idx: int):
        """Build a stable key even when moodle_cmid is missing (manual grade items)."""
        if activity.cmid is not None:
            return f"cmid:{activity.cmid}:{idx}"
        if activity.grade_item_id is not None:
            return f"gradeitem:{activity.grade_item_id}:{idx}"
        return f"idx:{idx}"

    @staticmethod
    def _should_trust_proposal_items(proposal_categories: list) -> bool:
        """
        Decide when proposal category item lists are authoritative.

        Initial generated proposals frequently place all activities under Assignments.
        In that case we should trust module/keyword rules more than proposal item lists.
        """
        non_empty = [c for c in proposal_categories if getattr(c, "items", None)]
        if len(non_empty) != 1:
            return True

        only_category = ContentMapper._normalize(getattr(non_empty[0], "name", ""))
        if only_category in {"assignment", "assignments"} and len(proposal_categories) > 1:
            return False
        return True
