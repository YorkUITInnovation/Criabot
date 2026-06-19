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
    CATEGORY_CONFIDENCE_THRESHOLD = 0.8
    SUBCATEGORY_CONFIDENCE_THRESHOLD = 0.7

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
        category_by_key = {
            str(category.name).strip().lower(): category
            for category in proposal_categories
            if str(category.name).strip()
        }

        # Pre-pass: build a name → (category, subcategory) index from proposal item placement.
        # Items explicitly placed in the proposal tree skip deterministic/LLM and get
        # confirmed category/subcategory pre-populated from the proposal.
        proposal_item_placement: dict[str, tuple[str, str]] = {}
        not_graded_keys = {
            str(item).strip().lower()
            for item in (getattr(proposal, "not_graded_items", None) or [])
            if str(item).strip()
        }
        for cat in proposal_categories:
            for item in (cat.items or []):
                key = str(item).strip().lower()
                if key:
                    proposal_item_placement[key] = (cat.name, "")
            for sub in (cat.subcategories or []):
                for item in (sub.items or []):
                    key = str(item).strip().lower()
                    if key:
                        proposal_item_placement[key] = (cat.name, sub.name)

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
            elif category_name and confidence < self.CATEGORY_CONFIDENCE_THRESHOLD:
                llm_candidates.append(activity)
            elif not category_name:
                llm_candidates.append(activity)

        # Stage 2: Call LLM only for low-confidence or unmatched items (cost control)
        llm_by_key = {}
        active_mapper_chat_id = mapper_chat_id
        if llm_candidates:
            llm_by_key, active_mapper_chat_id = await self._llm_assignments(
                llm_candidates,
                proposal,
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

            # Stage 2: Override with LLM category when deterministic match is low-confidence.
            llm_pick = llm_by_key.get(activity_key)
            if llm_pick is None and activity.cmid is not None:
                llm_pick = llm_by_key.get(activity.cmid)
            category_from_llm = False
            if llm_pick and (confidence < self.CATEGORY_CONFIDENCE_THRESHOLD or category_name == self.UNCATEGORIZED):
                category_name = llm_pick["category"]
                confidence = float(llm_pick.get("confidence", 0.75))
                reasoning = llm_pick.get("reasoning") or "Mapped using LLM category classification with course context."
                from_deterministic = False
                category_from_llm = True

            # Stage 3: Subcategory assignment only after a confident parent category is chosen.
            parent_category = category_by_key.get(str(category_name or "").strip().lower())
            subcategory_name, subcategory_reasoning = self._assign_subcategory_after_category(
                activity=activity,
                category_name=category_name,
                category_confidence=confidence,
                parent_category=parent_category,
                llm_pick=llm_pick if category_from_llm else None,
            )

            if subcategory_name and subcategory_reasoning and reasoning:
                reasoning = f"{reasoning} {subcategory_reasoning}".strip()
            elif subcategory_name and subcategory_reasoning:
                reasoning = subcategory_reasoning

            # Stage 4: When the activity is explicitly placed under a *subcategory*
            # in the proposal tree, honour that placement unconditionally — it is a
            # direct instructor instruction that is more authoritative than
            # deterministic/LLM category inference.  Category-level placement (no
            # subcategory) is already handled by _suggest_category Priority 1 with
            # the bias guard, so we leave those rows untouched here.
            activity_name_key = str(activity.name or "").strip().lower()
            placement = proposal_item_placement.get(activity_name_key)
            if activity_name_key in not_graded_keys:
                category_name = self.NOT_GRADED
                subcategory_name = ""
                confidence = 1.0
                reasoning = "Marked not graded per instructor instruction."
                from_deterministic = True
            elif placement and placement[1]:
                # placement[1] is the subcategory — non-empty means explicit placement
                pinned_cat, pinned_sub = placement
                category_name = pinned_cat
                subcategory_name = pinned_sub
                confidence = 1.0
                reasoning = "Category and subcategory pre-assigned from proposal item placement."
                from_deterministic = True

            item = {
                "moodle_cmid": activity.cmid,
                "grade_item_id": activity.grade_item_id,
                "module_type": activity.module,
                "itemtype": activity.itemtype or "mod",
                "activity_name": activity.name,
                "activity_key": activity_key,
                "item_source": item_type,  # "activity" or "manual" grade item
                "suggested_category": category_name,
                "confirmed_category": category_name,
                "suggested_subcategory": subcategory_name,
                "confirmed_subcategory": subcategory_name,
                "subcategory": subcategory_name,
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
        invalid_by_key = {}
        for item in validation_errors:
            activity_key = item.get("activity_key")
            if activity_key:
                invalid_by_key[activity_key] = item
            elif item.get("moodle_cmid") is not None:
                invalid_by_key[item.get("moodle_cmid")] = item
        if invalid_by_key:
            for item in mapping:
                activity_key = item.get("activity_key")
                cmid = item.get("moodle_cmid")
                if activity_key in invalid_by_key or cmid in invalid_by_key:
                    item["suggested_category"] = self.UNCATEGORIZED
                    item["confirmed_category"] = self.UNCATEGORIZED
                    item["suggested_subcategory"] = ""
                    item["confirmed_subcategory"] = ""
                    item["subcategory"] = ""
                    if item not in uncategorized:
                        uncategorized.append(item)

        invalid_sub_by_key = {}
        for issue in validation_errors:
            if not issue.get("missing_subcategory"):
                continue
            activity_key = issue.get("activity_key")
            if activity_key:
                invalid_sub_by_key[activity_key] = issue
            elif issue.get("moodle_cmid") is not None:
                invalid_sub_by_key[issue.get("moodle_cmid")] = issue
        if invalid_sub_by_key:
            for item in mapping:
                activity_key = item.get("activity_key")
                cmid = item.get("moodle_cmid")
                if activity_key in invalid_sub_by_key or cmid in invalid_sub_by_key:
                    item["suggested_subcategory"] = ""
                    item["confirmed_subcategory"] = ""
                    item["subcategory"] = ""

        # Post-pass: Add mapping rows for manual grade items from the proposal
        # that aren't already in the mapping (i.e., not from course_activities).
        activity_names_in_mapping = {
            str(item.get("activity_name") or "").strip().lower()
            for item in mapping
            if str(item.get("activity_name") or "").strip()
        }
        for category in proposal_categories:
            for manual_item in (category.items or []):
                item_key = str(manual_item).strip().lower()
                if item_key and item_key not in activity_names_in_mapping:
                    manual_row = {
                        "moodle_cmid": None,
                        "grade_item_id": None,
                        "module_type": None,
                        "itemtype": "manual",
                        "activity_name": str(manual_item).strip(),
                        "activity_key": f"manual:{item_key}",
                        "item_source": "manual",
                        "suggested_category": category.name,
                        "confirmed_category": category.name,
                        "suggested_subcategory": "",
                        "confirmed_subcategory": "",
                        "subcategory": "",
                        "finalized": False,
                        "confidence": 1.0,
                        "reasoning": "Manual grade item from proposal.",
                        "mapping_method": "manual",
                    }
                    mapping.append(manual_row)
            for sub in (category.subcategories or []):
                for manual_item in (sub.items or []):
                    item_key = str(manual_item).strip().lower()
                    if item_key and item_key not in activity_names_in_mapping:
                        manual_row = {
                            "moodle_cmid": None,
                            "grade_item_id": None,
                            "module_type": None,
                            "itemtype": "manual",
                            "activity_name": str(manual_item).strip(),
                            "activity_key": f"manual:{item_key}",
                            "item_source": "manual",
                            "suggested_category": category.name,
                            "confirmed_category": category.name,
                            "suggested_subcategory": sub.name,
                            "confirmed_subcategory": sub.name,
                            "subcategory": sub.name,
                            "finalized": False,
                            "confidence": 1.0,
                            "reasoning": "Manual grade item from proposal.",
                            "mapping_method": "manual",
                        }
                        mapping.append(manual_row)

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
        valid_subcategories_by_parent: dict[str, set[str]] = {}
        for category in proposal.categories or []:
            parent_key = str(category.name).strip().lower()
            if not parent_key:
                continue
            valid_subcategories_by_parent[parent_key] = {
                str(sub.name).strip().lower()
                for sub in (category.subcategories or [])
                if str(sub.name).strip()
            }

        issues: List[dict] = []
        for row in mapping_rows or []:
            suggested = str(row.get("suggested_category") or "").strip()
            if not suggested:
                continue
            if suggested in {self.UNCATEGORIZED, self.NOT_GRADED}:
                continue
            parent_key = suggested.lower()
            if parent_key not in valid_categories:
                issues.append(
                    {
                        "activity_key": row.get("activity_key"),
                        "moodle_cmid": row.get("moodle_cmid"),
                        "activity_name": row.get("activity_name"),
                        "missing_category": suggested,
                        "reason": "Category was removed, renamed, or not present in the current proposal.",
                    }
                )
                continue

            suggested_sub = str(
                row.get("suggested_subcategory") or row.get("subcategory") or ""
            ).strip()
            if not suggested_sub:
                continue

            valid_subs = valid_subcategories_by_parent.get(parent_key, set())
            if suggested_sub.lower() in valid_subs:
                continue

            issues.append(
                {
                    "activity_key": row.get("activity_key"),
                    "moodle_cmid": row.get("moodle_cmid"),
                    "activity_name": row.get("activity_name"),
                    "parent_category": suggested,
                    "missing_subcategory": suggested_sub,
                    "reason": "Subcategory was removed, renamed, or not present under the parent category.",
                }
            )
        return issues

    async def _llm_assignments(
        self,
        course_activities: List[CourseActivity],
        proposal: Optional[GradebookProposal],
        preferred_chat_id: Optional[str] = None,
        return_chat_id: bool = False,
    ) -> dict | tuple[dict, Optional[str]]:
        def _ret(data: dict, chat_id: Optional[str]) -> dict | tuple[dict, Optional[str]]:
            if return_chat_id:
                return data, chat_id
            return data

        proposal_categories = proposal.categories if proposal else []
        category_names = [category.name for category in proposal_categories]
        if not course_activities:
            logger.warning("No course activities provided for LLM assignments.")
            return _ret({}, preferred_chat_id)
        if not category_names:
            logger.warning("No category names provided for LLM assignments.")
            return _ret({}, preferred_chat_id)
        if self._criadex is None:
            logger.error("Criadex instance is not initialized. Cannot perform LLM assignments.")
            return _ret({}, preferred_chat_id)

        prompt = self._build_llm_prompt(course_activities, proposal_categories)
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
        for idx, row in enumerate(parsed):
            try:
                activity_key = str(row.get("activity_key") or "").strip()
                cmid = row.get("moodle_cmid")
                if activity_key == "" and cmid is None:
                    logger.warning("LLM response row missing both 'activity_key' and 'moodle_cmid'. Skipping row.")
                    continue
                category = str(row.get("category") or "").strip()
                canonical = valid_categories.get(category.lower())
                if not canonical:
                    logger.warning(f"Invalid category '{category}' in LLM response. Skipping row.")
                    continue
                confidence = float(row.get("confidence", 0.75))
                subcategory_raw = str(row.get("subcategory") or "").strip()
                subcategory_reasoning = str(row.get("subcategory_reasoning") or "").strip()
                subcategory_confidence = row.get("subcategory_confidence")
                if subcategory_confidence is None:
                    subcategory_confidence = confidence if subcategory_raw else 0.0
                else:
                    subcategory_confidence = float(subcategory_confidence)
                key = activity_key
                if key == "":
                    activity = course_activities[idx] if idx < len(course_activities) else None
                    if activity is None:
                        logger.warning("LLM response row could not be matched to a course activity index. Skipping row.")
                        continue
                    key = self._activity_key(activity, idx)
                value = {
                    "category": canonical,
                    "subcategory": subcategory_raw,
                    "confidence": confidence,
                    "subcategory_confidence": subcategory_confidence,
                    "reasoning": str(row.get("reasoning") or "Mapped using LLM category classification."),
                    "subcategory_reasoning": subcategory_reasoning,
                }
                out[key] = value
                if cmid is not None:
                    out[cmid] = value
            except Exception as e:
                logger.exception(f"Error processing LLM response row: {e}")
                continue

        return _ret(out, resolved_chat_id or preferred_chat_id)

    def _build_llm_prompt(self, course_activities: List[CourseActivity], proposal_categories: list) -> str:
        category_names = [category.name for category in proposal_categories]
        activity_rows = []
        for activity in course_activities:
            item_source = "Activity Module" if (activity.cmid and activity.module) else "Manual Grade Item"
            activity_rows.append({
                "activity_key": self._activity_key(activity, len(activity_rows)),
                "moodle_cmid": activity.cmid,
                "grade_item_id": activity.grade_item_id,
                "module": activity.module or "N/A",
                "activity_name": activity.name,
                "item_source": item_source,
            })

        subcategories_by_parent = {}
        for category in proposal_categories:
            parent_name = str(category.name).strip()
            if not parent_name:
                continue
            subcategories_by_parent[parent_name] = [
                str(sub.name).strip()
                for sub in (category.subcategories or [])
                if str(sub.name).strip()
            ]

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
            
            subcats = subcategories_by_parent.get(cat) or []
            sub_hint = ""
            if subcats:
                sub_hint = f" Subcategories: {', '.join(subcats)}."
            if hint:
                category_descriptions.append(f"- **{cat}**: {hint}{sub_hint}")
            else:
                category_descriptions.append(f"- **{cat}**: (no description provided){sub_hint}")

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
            "subcategories_by_parent": subcategories_by_parent,
            "category_descriptions": "\n".join(category_descriptions),
            "uncategorized": self.UNCATEGORIZED,
            "not_graded": self.NOT_GRADED,
            "activities": activity_rows,
            "keyword_reference": keyword_reference,
            "instructions": [
                "You are a Moodle gradebook expert following YorkU eClass standards.",
                "Step 1 — Category: analyze each grade item and assign ONE category from allowed_categories.",
                "Step 2 — Subcategory: only after choosing a category with confidence >= 0.8, "
                "optionally assign a subcategory from that same category's subcategories_by_parent entry.",
                "If the chosen category has no subcategories, return an empty subcategory string.",
                "If category confidence is below 0.8, return an empty subcategory string.",
                "Use the keyword reference above to guide your categorization.",
                "If an item name contains clear keywords (Quiz, Assignment, Midterm, etc.), prioritize that mapping.",
                "For Activity Modules (course activities with modules), check module type: quiz→Quizzes, assign→Assignments, etc.",
                f"If an item genuinely doesn't fit any category, use '{self.UNCATEGORIZED}' so professor can manually review.",
                f"Use '{self.NOT_GRADED}' ONLY for administrative items (announcements, resources, links) with zero grade points.",
                "For subcategories: use an exact allowed subcategory name only when subcategory_confidence is >= 0.7.",
                "If no subcategory is a clear fit, return an empty string for subcategory (parent category only).",
                "Never assign a subcategory from a different parent category.",
                "DO NOT default everything to 'Assignments' — be precise and use keyword matching.",
                "Return ONLY a JSON array with objects: "
                "{activity_key (string), moodle_cmid (int|null), category (string), subcategory (string), "
                "confidence (0.5-1.0), subcategory_confidence (0.0-1.0), reasoning (string), subcategory_reasoning (string)}.",
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

    def _category_assignment_confident(self, category_name: Optional[str], confidence: float) -> bool:
        if not category_name:
            return False
        if category_name in {self.UNCATEGORIZED, self.NOT_GRADED}:
            return False
        return float(confidence) >= self.CATEGORY_CONFIDENCE_THRESHOLD

    @staticmethod
    def _parent_has_subcategories(parent_category) -> bool:
        if parent_category is None:
            return False
        subcategories = getattr(parent_category, "subcategories", None) or []
        return any(str(getattr(sub, "name", "") or "").strip() for sub in subcategories)

    def _resolve_subcategory_for_parent(self, parent_category, subcategory_name: str) -> str:
        if parent_category is None:
            return ""
        valid_subs = {
            str(sub.name).strip().lower(): str(sub.name).strip()
            for sub in (getattr(parent_category, "subcategories", None) or [])
            if str(sub.name).strip()
        }
        if not valid_subs:
            return ""
        raw = str(subcategory_name or "").strip()
        if not raw:
            return ""
        canonical = valid_subs.get(raw.lower())
        if canonical:
            return canonical
        for candidate_key, candidate_name in valid_subs.items():
            if raw.lower() in candidate_key or candidate_key in raw.lower():
                return candidate_name
        return ""

    def _assign_subcategory_after_category(
        self,
        activity: CourseActivity,
        category_name: Optional[str],
        category_confidence: float,
        parent_category,
        llm_pick: Optional[dict] = None,
    ) -> tuple[str, str]:
        """
        Assign a subcategory only after a confident parent category is resolved.

        Skips subcategory mapping when the parent category is uncertain, has no
        subcategories, or when a suggested subcategory does not belong to that parent.
        """
        if not self._category_assignment_confident(category_name, category_confidence):
            return "", ""
        if not self._parent_has_subcategories(parent_category):
            return "", ""

        subcategory_name = ""
        subcategory_reasoning = ""

        if llm_pick:
            raw_subcategory = str(llm_pick.get("subcategory") or "").strip()
            subcategory_confidence = float(
                llm_pick.get("subcategory_confidence", llm_pick.get("confidence", 0.0))
            )
            if raw_subcategory and subcategory_confidence >= self.SUBCATEGORY_CONFIDENCE_THRESHOLD:
                resolved = self._resolve_subcategory_for_parent(parent_category, raw_subcategory)
                if resolved:
                    subcategory_name = resolved
                    subcategory_reasoning = str(llm_pick.get("subcategory_reasoning") or "").strip()

        if not subcategory_name:
            deterministic_sub, sub_confidence, deterministic_reason = self._suggest_subcategory(
                activity,
                str(category_name or ""),
                parent_category,
            )
            if deterministic_sub and sub_confidence >= self.SUBCATEGORY_CONFIDENCE_THRESHOLD:
                subcategory_name = deterministic_sub
                subcategory_reasoning = deterministic_reason

        return subcategory_name, subcategory_reasoning

    def _suggest_subcategory(
        self,
        activity: CourseActivity,
        parent_category_name: str,
        parent_category,
    ) -> tuple[str, float, str]:
        subcategories = list(getattr(parent_category, "subcategories", None) or [])
        if not subcategories:
            return "", 0.0, ""

        normalized_name = self._normalize(activity.name)
        best_name = ""
        best_score = 0.0
        best_reason = ""

        for subcategory in subcategories:
            sub_name = str(subcategory.name).strip()
            if not sub_name:
                continue
            sub_norm = self._normalize(sub_name)
            if sub_norm and sub_norm in normalized_name:
                score = 0.92
                if score > best_score:
                    best_name = sub_name
                    best_score = score
                    best_reason = f"Activity name contains subcategory '{sub_name}'."
                continue

            for token in sub_norm.split():
                if len(token) < 4:
                    continue
                if token in normalized_name:
                    score = 0.86
                    if score > best_score:
                        best_name = sub_name
                        best_score = score
                        best_reason = f"Matched subcategory token '{token}' in activity name."

        if best_score >= self.SUBCATEGORY_CONFIDENCE_THRESHOLD:
            return best_name, best_score, best_reason
        return "", 0.0, ""

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
