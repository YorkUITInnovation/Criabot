from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import List, Dict, Optional

from .schemas import (
    CourseActivity,
    GradebookCategory,
    GradebookProposal,
    GradebookSessionRecord,
    GRADE_DISPLAY_TYPE_NAMES,
)
from .proposal import ProposalGenerator, validate_proposal_weights, _proposal_tracked_item_names
from .formula_parser import FormulaParser


def check_weight_warnings(proposal: GradebookProposal) -> List[str]:
    """Check for weight warnings (non-blocking) based on aggregation method.
    
    - Method 13 (Natural): Warn if total > 100% or < 50% (suggests incomplete setup)
    - Other methods: No warnings (errors are handled by validate_proposal_weights)
    """
    warnings = []
    method = int(getattr(proposal, "aggregation_method", 13))
    
    if method == 13:  # Natural aggregation
        total_weight = sum(float(getattr(cat, "weight", 0.0)) for cat in (proposal.categories or []))
        if total_weight > 100.1:
            warnings.append(
                f"⚠️ Total weight is {total_weight:.1f}%, which exceeds 100%. "
                f"This can cause unexpected behavior in some gradebook configurations. "
                f"Consider adjusting weights to 100% if possible."
            )
    
    return warnings


class ConversationManager:
    @staticmethod
    def _mapping_row_for_item(item_name: str, mapping_rows: List[dict]) -> Optional[dict]:
        name_key = str(item_name or "").strip().lower()
        if not name_key:
            return None
        for row in mapping_rows:
            if str(row.get("activity_name") or "").strip().lower() == name_key:
                return row
        return None

    @staticmethod
    def _is_manual_mapping_row(row: Optional[dict]) -> bool:
        if not row:
            return False
        source = str(row.get("item_source") or "").strip().lower()
        itemtype = str(row.get("itemtype") or row.get("grade_item_type") or "").strip().lower()
        return source in {"manual", "proposal_manual", "proposal"} or itemtype == "manual"

    @staticmethod
    def _match_course_activity(item_name: str, course_activities: List[CourseActivity]) -> Optional[CourseActivity]:
        query_key = str(item_name or "").strip().lower()
        if not query_key:
            return None
        for activity in course_activities:
            name_key = str(getattr(activity, "name", "") or "").strip().lower()
            if name_key == query_key:
                return activity
        return None

    @staticmethod
    def _course_activity_is_moodle_activity(activity: CourseActivity) -> bool:
        module = str(getattr(activity, "module", None) or "").strip()
        cmid = getattr(activity, "cmid", None)
        itemtype = str(getattr(activity, "itemtype", None) or "mod").strip().lower()
        if itemtype == "manual" and not module and cmid in (None, "", 0):
            return False
        return bool(
            module
            or itemtype == "mod"
            or (cmid is not None and str(cmid).strip() not in {"", "0"})
        )

    @staticmethod
    def _is_moodle_activity_item(
        item_name: str,
        course_activities: List[CourseActivity],
        mapping_rows: Optional[List[dict]] = None,
    ) -> bool:
        mapping_row = ConversationManager._mapping_row_for_item(item_name, mapping_rows or [])
        if ConversationManager._is_manual_mapping_row(mapping_row):
            return False

        activity = ConversationManager._match_course_activity(item_name, course_activities)
        if activity is None:
            return False
        return ConversationManager._course_activity_is_moodle_activity(activity)

    @staticmethod
    def _contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
        text_l = text.lower()
        for phrase in phrases:
            # Use word boundary for single-token phrases (e.g., "no")
            # to avoid false matches like "now".
            if " " in phrase:
                if phrase in text_l:
                    return True
            else:
                if re.search(rf"\b{re.escape(phrase)}\b", text_l):
                    return True
        return False

    @staticmethod
    def _looks_like_gradebook_instruction(text: str) -> bool:
        if "%" in text:
            return True
        keywords = (
            "create a gradebook",
            "gradebook with",
            "assignments",
            "labs",
            "midterm",
            "final exam",
            "weights",
            "category",
            "grade item",
            "manual grade item",
            "activity",
        )
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _looks_like_context_signal(text: str) -> bool:
        context_tokens = (
            "syllabus",
            "uploaded",
            "upload",
            "course outline",
            "grading policy",
            "assessment",
            "here is",
            "attached",
        )
        return any(token in text for token in context_tokens)

    @staticmethod
    def _looks_like_upload_signal(text: str) -> bool:
        text_l = text.lower()
        upload_tokens = (
            "uploaded",
            "upload",
            "attached",
            "attachment",
            "syllabus",
            "supporting document",
            "support document",
            "course outline",
            "grading policy",
        )
        return any(token in text_l for token in upload_tokens)

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Detect if user is asking a question rather than providing instructions."""
        text_l = text.lower().strip()
        if "?" in text_l:
            return True

        # Interrogative starters (at beginning) to avoid false positives
        # for phrases like "use what I gave you".
        starters = (
            "what ", "how ", "why ", "when ", "where ", "which ",
            "can you", "could you", "should ", "tell me", "explain",
            "do you", "did you", "is there", "are there",
        )
        return text_l.startswith(starters)

    @staticmethod
    def _looks_like_affirmation(text: str) -> bool:
        """Detect if user is confirming/approving."""
        affirmation_words = ("yes", "yeah", "yep", "fine", "good", "looks good", "ok", "okay", "approved", "accept", "proceed", "confirmed", "correct")
        return ConversationManager._contains_phrase(text, affirmation_words)

    @staticmethod
    def _looks_like_rejection(text: str) -> bool:
        """Detect if user is rejecting or asking for changes."""
        rejection_words = ("no", "nope", "change", "modify", "adjust", "different", "wrong", "bad", "don't like", "not right", "refine", "revise", "again")
        return ConversationManager._contains_phrase(text, rejection_words)

    @staticmethod
    def _looks_like_proposal_request(text: str) -> bool:
        request_markers = (
            "give me a proposal",
            "give me proposal",
            "give proposal",
            "proposal based on what you know",
            "give me proposal based on what you know",
            "give me a proposal based on what you know",
            "show proposal",
            "generate proposal",
            "build proposal",
            "create proposal",
            "make proposal",
            "craft proposal",
            "propose",
        )
        text_l = text.lower()
        return any(marker in text_l for marker in request_markers)

    @staticmethod
    def _looks_like_fresh_generation_request(text: str) -> bool:
        text_l = text.lower()
        markers = (
            "start fresh",
            "fresh generation",
            "generate from syllabus",
            "ignore baseline",
            "do not use baseline",
            "dont use baseline",
            "use syllabus only",
        )
        return any(marker in text_l for marker in markers)

    @staticmethod
    def _detect_aggregation_method(text: str) -> int | None:
        """Detect aggregation method preference only when aggregation intent is explicit."""
        text_l = text.lower()

        has_agg_context = bool(
            re.search(r"\b(?:aggregation|aggregate|method|grade\s+aggregation)\b", text_l)
            or re.search(r"\b(?:use|set|switch|change)\b", text_l)
            or any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted", "mean of grades", "simple mean", "natural"))
        )

        if has_agg_context and any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted")):
            if "simple" in text_l:
                return 11
            return 10

        if has_agg_context and any(term in text_l for term in ("mean of grades", "simple mean")):
            if "extra credit" in text_l or "extra credits" in text_l:
                return 12
            return 0

        if has_agg_context and ("extra credit" in text_l or "extra credits" in text_l):
            return 12

        if has_agg_context and any(term in text_l for term in ("natural", "moodle default", "default aggregation")):
            return 13

        return None

    def next_phase(self, session: GradebookSessionRecord, prompt: str) -> str:
        text = prompt.lower().strip()
        extraction = session.extraction or {}
        has_syllabus = bool(extraction.get("has_syllabus"))

        # Help/capability prompts should not alter phase.
        if self._looks_like_help_request(text):
            return session.phase

        # Read-only view/listing commands should not alter phase once editing has started.
        if session.phase in {"PROPOSAL", "REFINEMENT", "ACCEPTED", "COMPLETED"}:
            if self._looks_like_proposal_view_request(text) or self._looks_like_listing_request(text):
                return session.phase

        # Explicit proposal requests should not be interpreted as acceptance.
        if self._looks_like_proposal_request(text):
            if session.phase in {"INTAKE", "ANALYSIS", "BASELINE_READY", "PROPOSAL", "REFINEMENT", "ACCEPTED", "COMPLETED"}:
                return "PROPOSAL"

        # Check for acceptance keywords
        if self._looks_like_affirmation(text):
            if session.phase in ["PROPOSAL", "REFINEMENT"]:
                proposal = session.proposal
                proposal_errors = validate_proposal_weights(proposal) if proposal else [{"path": ["proposal"]}]
                has_weight_error = any(err.get("path") == ["proposal"] for err in proposal_errors)
                if not has_weight_error:
                    return "ACCEPTED"
                else:
                    return session.phase

        # Check for rejection/refinement keywords
        if self._looks_like_rejection(text):
            if session.phase in {"PROPOSAL", "ACCEPTED", "COMPLETED"}:
                return "REFINEMENT"
            return session.phase

        # Questions during flow: stay in current phase, respond naturally
        if self._looks_like_question(text):
            if session.phase in {"ACCEPTED", "COMPLETED"}:
                if self._looks_like_gradebook_refinement(text) or self._looks_like_gradebook_instruction(text):
                    return "REFINEMENT"
                if session.phase == "COMPLETED" and text:
                    return "REFINEMENT"
            return session.phase

        # Phase transitions based on current state
        if session.phase == "INTAKE":
            if has_syllabus:
                if self._looks_like_proposal_request(text) or self._looks_like_affirmation(text):
                    return "PROPOSAL"
                return "ANALYSIS"
            # If no syllabus detected: ask for it or request input
            if self._looks_like_gradebook_instruction(text) or self._looks_like_proposal_request(text):
                return "PROPOSAL"
            if self._looks_like_context_signal(text):
                return "ANALYSIS"
            # Stay in INTAKE if user hasn't provided syllabus/content yet
            return "INTAKE"

        if session.phase == "ANALYSIS":
            return "PROPOSAL"

        if session.phase == "BASELINE_READY":
            if self._looks_like_fresh_generation_request(text):
                return "ANALYSIS" if has_syllabus else "INTAKE"
            if self._looks_like_gradebook_refinement(text) or self._looks_like_gradebook_instruction(text):
                return "PROPOSAL"
            if self._looks_like_affirmation(text) or self._looks_like_proposal_request(text):
                return "PROPOSAL"
            return "BASELINE_READY"

        if session.phase == "REFINEMENT":
            return "REFINEMENT"

        if session.phase == "PROPOSAL":
            return "REFINEMENT"  # Default to refinement if unclear

        if session.phase in {"ACCEPTED", "COMPLETED"}:
            # New uploaded context should restart proposal workflow.
            if self._looks_like_context_signal(text):
                return "ANALYSIS"
            if self._looks_like_proposal_request(text):
                return "PROPOSAL"
            if self._looks_like_gradebook_refinement(text) or self._looks_like_gradebook_instruction(text):
                return "REFINEMENT"

            # After finalization, any non-question freeform text is treated as
            # an edit intent so the user can continue refining in-place.
            if session.phase == "COMPLETED" and text:
                return "REFINEMENT"

        return session.phase

    @staticmethod
    def _is_read_only_gradebook_prompt(text: str) -> bool:
        """Return True when the prompt should only render information, not mutate the proposal."""
        if not text or not str(text).strip():
            return False
        return (
            ConversationManager._looks_like_help_request(text)
            or ConversationManager._looks_like_proposal_view_request(text)
            or ConversationManager._looks_like_listing_request(text)
            or ConversationManager._looks_like_proposal_request(text)
            or bool(re.search(r"\b(?:undo|redo)\b", text.lower()))
        )

    @staticmethod
    def _looks_like_gradebook_refinement(text: str) -> bool:
        """Return True if the prompt appears to contain a valid gradebook refinement request."""
        if ConversationManager._is_read_only_gradebook_prompt(text):
            return False
        t = text.lower()
        # Explicit weight change indicators.
        if "%" in t:
            return True
        if re.search(r"\b\d+(\.\d+)?\s*%", t):
            return True
        # Formula-only prompts (with or without explicit keywords) are valid refinements.
        if ConversationManager._extract_formula_from_prompt(text):
            return True
        if re.search(r"\b(?:clear|remove|delete|unset)\b.*\bformula\b", t):
            return True
        if re.search(r"\b(?:include|exclude)\s+empty\b", t):
            return True
        if re.search(r"\b(?:include|exclude|ignore|aggregate)\s+outcomes?\b", t):
            return True
        # Action verbs must be accompanied by gradebook context or numeric targets
        # to avoid false positives like "make me a pizza".
        if re.search(r"\b(?:set|make|change|adjust|update|increase|decrease|give|assign|replace|apply|use)\b", t):
            has_numeric_target = bool(re.search(r"\b\d+(?:\.\d+)?\s*%?\b", t))
            has_gradebook_context = bool(re.search(
                r"\b(?:assignments?|labs?|midterm|final(?:\s+exam)?|quizzes?|projects?|participation|category|categories|weight|weights|aggregation|method|formula|gradebook|grade\s+item|manual\s+grade\s+item|activity|activities|drop|keep|hide|show|rename|split|subcategor(?:y|ies))\b",
                t,
            ))
            if has_numeric_target or has_gradebook_context:
                return True
        # Structural modifications.
        refinement_keywords = (
            "split", "divide", "subcategor", "rename", "remove", "delete", "add", "added",
            "hide", "hidden", "show", "unhide", "reveal", "lock", "unlock",
            "drop", "keep", "extra credit", "aggregat", "method", "weighted",
            "natural", "mean", "weight", "rebalance", "redistribute", "proportion",
            "normalize", "swap",             "move", "to category", "grade item", "manual grade item",
            "not graded", "ungraded", "don't grade", "do not grade",
        )
        for kw in refinement_keywords:
            if kw in t:
                return True
        # Direct method number: "number 13", "method 13".
        if re.search(r'\b(?:method\s+(?:number\s+)?|number\s+)\d+\b', t):
            return True
        # Dates (ISO or relative).
        if re.search(r'\b\d{4}-\d{2}-\d{2}\b', t):
            return True
        if re.search(r'\b(?:next\s+week|tomorrow|next\s+month|weeks?\s+from\s+now|in\s+\d+\s+days?)\b', t):
            return True
        return False

    @staticmethod
    def _looks_like_formula_request(text: str) -> bool:
        """Detect if user is asking about or requesting formula support."""
        t = text.lower()
        formula_keywords = (
            "formula", "calculate", "use this formula", "equation", "set.*as",
            "computation", "compute", "final grade.*formula", "grade calculation",
        )
        return any(kw in t for kw in formula_keywords)

    @staticmethod
    def _looks_like_help_request(text: str) -> bool:
        """Detect direct help/capabilities requests even without question punctuation."""
        t = text.lower().strip()
        if not t:
            return False

        direct_terms = {
            "help",
            "show help",
            "commands",
            "supported commands",
            "what can you do",
            "what do you support",
            "what instruction do you support",
            "what instructions do you support",
            "show supported commands",
        }
        if t in direct_terms:
            return True

        patterns = (
            r"\bwhat\s+can\s+you\s+do\b",
            r"\bwhat\s+(?:do\s+you\s+)?support\b",
            r"\b(?:which|what)\s+instructions?\s+do\s+you\s+support\b",
            r"\bsupported\s+commands?\b",
            r"\bhelp\b",
        )
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _looks_like_listing_request(text: str) -> bool:
        """Detect requests to show grade items, activities, or both."""
        t = text.lower().strip()
        if not t:
            return False

        # Allow optional quantifiers/articles between verb and noun: 'show all', 'list all', 'show the'
        patterns = (
            r"\b(?:show|list|display)\s+(?:(?:all|the|my|full)\s+)*(?:grade\s+items?|manual\s+grade\s+items?|activities?|activity\s+and\s+grade\s+item|grade\s+item\s+and\s+activity)\b",
        )
        return any(re.search(pattern, t) for pattern in patterns)

    @staticmethod
    def _parse_listing_request_options(text: str) -> Optional[Dict[str, bool]]:
        """Parse listing scope/fullness options from a listing-style prompt."""
        t = text.lower().strip()
        if not t or not ConversationManager._looks_like_listing_request(t):
            return None

        wants_grade_items = bool(re.search(r"\b(?:manual\s+)?grade\s+items?\b", t))
        wants_activities = bool(re.search(r"\bactivities?\b", t))
        show_all = bool(
            re.search(r"\b(?:show|list|display)\s+all\b", t)
            or re.search(r"\bfull\s+list\b", t)
            or re.search(r"\bshow\s+more\b", t)
        )

        include_grade_items = wants_grade_items or not wants_activities
        include_activities = wants_activities or not wants_grade_items

        # "show grade items" should return full category item lists by default.
        if wants_grade_items and not wants_activities:
            show_all = True

        return {
            "include_activities": include_activities,
            "include_grade_items": include_grade_items,
            "show_all": show_all,
        }

    @staticmethod
    def _parse_proposal_view_request(text: str) -> Optional[str]:
        """Detect read-only proposal/mapping view commands."""
        t = text.lower().strip()
        if not t or ConversationManager._looks_like_listing_request(t):
            return None

        if re.search(r"\b(show|display|check|view|what(?:'s| is))\b.*\b(sync\s+status|mapping\s+sync)\b", t):
            return "mapping_sync"
        if re.search(r"\b(sync\s+status|mapping\s+sync)\b", t) and re.search(
            r"\b(show|display|check|view|what(?:'s| is))\b", t
        ):
            return "mapping_sync"

        if re.search(r"\b(show|display|list|print)\b.*\bmapping\s+rows?\b", t):
            return "mapping_rows"
        if re.search(r"\bmapping\s+rows?\b", t) and re.search(r"\bbefore\s+finalize\b", t):
            return "mapping_rows"

        if re.search(r"\b(show|display)\b.*\bproposal\b.*\bmarkdown\b", t) or re.search(
            r"\bproposal\b.*\bas\s+markdown\b", t
        ):
            return "markdown_tree"

        if re.search(r"\b(show|display)\b.*\b(full\s+)?(proposal|gradebook\s+structure)\b", t):
            return "full_proposal"
        if re.search(r"\bshow\s+proposal\b", t):
            return "full_proposal"

        return None

    @staticmethod
    def _looks_like_proposal_view_request(text: str) -> bool:
        return ConversationManager._parse_proposal_view_request(text) is not None

    @staticmethod
    def _extract_formula_from_prompt(text: str) -> Optional[Dict]:
        """
        Extract formula from user prompt if present.

        Returns:
            Dict with formula details if detected, None otherwise.
        """
        return FormulaParser.extract_formula_and_detect(text)

    @staticmethod
    def _unsupported_request_warning() -> str:
        return (
            "⚠ I couldn't understand that request. Try one of these:\n\n"
            "- **Set weights:** Set Labs to 20%\n"
            "- **Add category:** Add Quizzes (or Added Quizzes)\n"
            "- **Add with weight:** Add Quizzes 10%\n"
            "- **Aggregation:** Use weighted mean or Use mean\n"
            "- **Split categories:** In Labs, split into Lab Reports 10%, In-Lab 5%\n"
            "- **Even split:** Split Quizzes into Midterm Quiz and Final Quiz\n"
            "- **Subcategories:** drop Projects, rename Projects to Project, or In Assignments, add subcategory Reflection 5%\n"
            "- **Subcategory share:** Assign 0.4 to Midterm Quiz (0.4 = 40% of the parent category)\n"
            "- **Formula:** Set Assignments formula to =average([[hw1]],[[hw2]])\n"
            "- **Rules/visibility:** Hide Midterm until 2026-05-19 or Drop lowest 1 from Assignments\n"
            "- **Grade items:** Add grade item X to Final Exam\n"
            "- **Activities:** Move quiz 1 to Assignments or Set Homework 1 to not graded\n"
            "- **Listing:** Show proposal, Show grade items, Show activities, Show activities and grade items\n"
            "- **Grade item edits:** remove manual grade item X, rename grade item X to Y\n"
            "- **Finalize:** Accept proposal and generate mapping\n"
            "- **Mapping fix:** Add a manual grade item for Final Exam in mapping\n"
            "- **Undo/redo:** undo or redo\n\n"
            "Tip: type **help** to see the full supported instruction list."
        )

    @staticmethod
    def _supported_instructions_help_text() -> str:
        return (
            "Here are supported instructions you can use:\n\n"
            "**Proposal layout and sync views**\n"
            "- Show proposal — full structure with totals, effects, notes, and aggregation\n"
            "- Show proposal as markdown — hierarchy only (categories, subcategories, and items)\n"
            "- Show activities and grade items — includes activity/manual labels and mapping-ready overview\n"
            "- Show mapping sync status — checks category/item alignment with current mapping rows\n"
            "- Show mapping rows before finalize — prints mapping rows to verify cmid/category/subcategory before apply\n\n"
            "**Syllabus / supporting docs**\n"
            "- Use my uploaded syllabus and give me a proposal\n"
            "- Analyze the latest uploaded supporting document and regenerate proposal\n"
            "- I uploaded a new file, use it and rebuild the gradebook proposal\n\n"
            "**Weights and categories**\n"
            "- Set Assignments 35%, Labs 15%, Midterm 20%, Final 30%\n"
            "- Change Labs to 20% and rebalance automatically\n"
            "- Rename Labs to Laboratory\n"
            "- Add Quizzes — creates the category at 0% so you can rebalance later\n"
            "- Add Projects 15% — creates the category with that weight immediately\n"
            "- Added Quizzes — shorthand phrasing also works\n\n"
            "**Manual grade items**\n"
            "- **Subcategory** = grouping bucket inside a parent category; **grade item** = concrete scored row inside a category or subcategory\n"
            "- Add grade item X to Final Exam — creates a manual grade item inside a category\n"
            "- Add grade item Reflection Journal 1 to Assignments\n"
            "- In Assignments, add subcategory Reflection 5% — or omit weight to start at 0%\n"
            "- Show grade items — lists proposal items inside each category\n"
            "- Show all grade items — lists all proposal grade items without truncation\n"
            "- remove manual grade item X or remove X from Final Exam — removes a manual grade item only\n"
            "- rename grade item X to Y — renames a grade item in place\n\n"
            "**Moodle activities**\n"
            "- Show activities — lists Moodle activities only\n"
            "- Show all activities — lists the complete Moodle activity list without truncation\n"
            "- Move quiz 1 to Final Exam — reassigns a Moodle activity to a different category\n"
            "- Move quiz 2 from Quizzes to Assignments — explicit source and target\n"
            "- Move quiz 1 to Homework — assigns a mapped activity to a subcategory under its parent\n"
            "- Move quiz 1 to Homework in Assignments — scoped subcategory assignment\n"
            "- Clear subcategory for quiz 1 or move quiz 1 to parent category — removes subcategory placement\n"
            "- Set weight of quiz 1 to 30% — overrides an activity's weight within its category\n"
            "- quiz 1 weight 30% — shorthand weight update\n"
            "- Set Homework 1 to not graded — marks the activity as not graded (removed from proposal categories; mapping syncs to not graded)\n"
            "- Moodle activities cannot be removed; move them, reassign them, set them to Not graded, or change their weight instead\n"
            "Note: category moves affect proposal items; subcategory moves update mapping rows when mapping exists.\n\n"
            "**Remove / drop a category**\n"
            "- Remove Quizzes — frees the weight (total decreases; you can reallocate later)\n"
            "- Drop Labs evenly — removes Labs and spreads its weight equally across remaining categories\n"
            "- Delete Midterm and give weight to Final Exam — removes Midterm and adds its weight to Final Exam\n"
            "- Remove Assignments and split weight among Labs and Midterm — splits weight equally between the two targets\n\n"
            "**Aggregation method**\n"
            "- Use weighted mean of grades\n"
            "- Switch to Natural\n"
            "- Show aggregation methods\n\n"
            "**Excel-style formulas**\n"
            "- Set Assignments formula to `=average([[hw1]],[[hw2]],[[project]])`\n"
            "- Use formula `=([[midterm]]*0.4)+([[final]]*0.6)` for Final Exam\n"
            "- Clear formula from Labs\n"
            "**Item references:** use `[[item_id]]` (legacy `[item]` is also accepted).\n"
            "**Separator:** use comma `,` between arguments (YorkU standard).\n\n"
            "If a formula is invalid, I'll return a direct warning and ask you to retry.\n"
            "Formula help: [YorkU custom formula guide](https://lthelp.yorku.ca/gradebook/creating-a-custom-formula)\n"
            "Excel help: [Excel formula reference](https://support.microsoft.com/excel)\n\n"
            "**Split subcategories**\n"
            "- In Quizzes, split into Midterm Quiz and Final Quiz — weights divided evenly (e.g. 12.5% each if parent is 25%)\n"
            "- Split Quizzes into Midterm Quiz with weight 0.4 and Final Quiz with weight 0.6 — fractions of parent weight\n"
            "- Assign 0.4 to Midterm Quiz — updates a subcategory share (0.4 × parent weight = 10% when parent is 25%)\n"
            "- Assign 0.6 to Final Quiz — once all shares sum to 1.0, the split warning clears\n\n"
            "**Subcategory edits (add / remove / rename / weight)**\n"
            "- drop Projects or remove Projects from Assignments — removes a subcategory and redistributes weight to siblings\n"
            "- drop Projects evenly — same, but explicitly spreads weight across remaining subcategories\n"
            "- rename Projects to Project or In Assignments, rename Projects to Project\n"
            "- In Assignments, add subcategory Reflection 5% or add Reflection 5% to Assignments\n"
            "- In Assignments, add subcategory Reflection — creates Reflection with 0% until you set a weight\n"
            "- set Homework weight to 12% — updates one subcategory share inside its parent\n"
            "- set weight of Lab Reports in Labs to 5% — scoped subcategory weight (parent name disambiguates)\n"
            "- set Lab Reports subcategory weight to 5% — explicit subcategory keyword\n"
            "- set weight of quiz 1 in Quizzes to 40% — scoped grade-item weight inside a category\n\n"
            "**Rules and visibility**\n"
            "- Drop lowest 1 from Assignments\n"
            "- Keep highest 2 from Labs\n"
            "- Hide Midterm until 2026-05-19\n"
            "- For Assignments, exclude empty grades or Include empty grades for Labs\n\n"
            "**Finalize flow**\n"
            "- Accept proposal and generate mapping\n"
            "- Show mapping rows before finalize\n"
            "- Add a manual grade item for Midterm in mapping\n"
            "- If a category has no activity row, map an existing activity, remove the category, or add a manual grade item in the mapping UI\n"
            "- Finalize now"
        )

    def make_reply(self, session: GradebookSessionRecord, proposal: GradebookProposal | None, prompt: str = "") -> str:
        text = (prompt or "").lower().strip()
        extraction = session.extraction or {}
        has_syllabus = bool(extraction.get("has_syllabus"))

        # Explicit effects command (works without question mark).
        if proposal is not None and re.search(r"\b(show|display|list)\b.*\beffects?\b|\bcurrent\s+effects?\b", text):
            full = bool(re.search(r"\b(all|detailed|detail)\b", text))
            effects_block = self._format_effects(proposal, max_display=999 if full else 6)
            if not effects_block:
                return "No effects recorded yet."
            if full:
                return f"Current effects (newest first):{effects_block}"
            return f"Recent effects (newest first):{effects_block}\n\nSay 'show all effects' to display everything."

        # Explicit aggregation-method list command (works without question mark).
        if re.search(r"\b(list|show|display|give\s+me)\b.*\b(grade\s+)?aggregation\s+methods?\b", text):
            return self._aggregation_methods_help_text()

        # Direct capability/help command (works with short prompts like "help").
        if text and self._looks_like_help_request(text):
            return self._supported_instructions_help_text()

        if text:
            listing_options = self._parse_listing_request_options(text)
            if listing_options:
                return self._format_activity_and_grade_item_summary(session, proposal, **listing_options)

        view_request = self._parse_proposal_view_request(text) if text else None
        if view_request:
            return self._reply_for_proposal_view(session, proposal, view_request)

        # Handle questions: answer them without forcing phase transitions
        if text and self._looks_like_question(text):
            answer = self._answer_question(session, text, proposal)
            if answer:
                return answer

        # Uploaded/attached context should trigger analysis guidance, not unsupported warnings.
        if text and self._looks_like_upload_signal(text) and not self._looks_like_proposal_request(text):
            return (
                "Thanks, I received your syllabus/supporting document. "
                "I'll analyze it and use it to improve your gradebook proposal. "
                "If you're ready, say 'show proposal' or ask for specific refinements."
            )

        # In proposal/refinement phases, reject clearly off-topic/non-action text
        # instead of re-rendering proposal as if the input were valid.
        if session.phase in {"PROPOSAL", "REFINEMENT"} and text:
            is_action = (
                self._looks_like_gradebook_refinement(text)
                or self._extract_formula_from_prompt(text) is not None
                or self._looks_like_affirmation(text)
                or self._looks_like_proposal_request(text)
                or self._looks_like_proposal_view_request(text)
                or self._looks_like_listing_request(text)
                or self._looks_like_help_request(text)
                or self._looks_like_question(text)
                or self._looks_like_upload_signal(text)
                or re.search(r"\b(undo|redo)\b", text)  # Allow undo/redo to pass through
            )
            if not is_action:
                return self._unsupported_request_warning()

        if session.phase == "INTAKE":
            if has_syllabus:
                return (
                    "I can see syllabus/context is available. "
                    "I'll use it to build your proposal. "
                    "If you're ready, say 'give me a proposal'."
                )
            # Check if user provided syllabus or grading info
            if text and (self._looks_like_context_signal(text) or self._looks_like_gradebook_instruction(text)):
                return (
                    "Thank you for sharing your materials. "
                    "I'm analyzing the grading structure and course activities to build a proposal. "
                    "This will take a moment..."
                )
            return (
                "I don't see a syllabus in your course yet. "
                "To build an accurate gradebook, I need information about your grading structure. "
                "You can:\n"
                "• Upload your syllabus (PDF or document)\n"
                "• Share the grading breakdown in this chat\n"
                "• Describe your assessment categories (assignments, quizzes, exams, etc.)\n\n"
                "How would you like to proceed?"
            )

        if session.phase == "ANALYSIS":
            return (
                "I found your grading structure and course activities. "
                "Let me analyze how to organize them into a gradebook proposal. "
                "I'm processing this information..."
            )

        if session.phase == "BASELINE_READY":
            return (
                "I captured your existing Moodle gradebook as the baseline and it is ready for refinement. "
                "Say 'show proposal' to continue with this baseline, or 'start fresh' to regenerate from syllabus context."
            )

        if session.phase == "PROPOSAL" and proposal:
            # For formula parsing failures, return a focused warning instead of re-rendering full proposal.
            formula_requested = self._extract_formula_from_prompt(prompt) is not None
            proposal_changed = bool((session.extraction or {}).get("proposal_changed"))
            if formula_requested and proposal_changed:
                formula_error = self._formula_error_reply(proposal)
                if formula_error:
                    return formula_error

            return self._build_full_proposal_reply(session, proposal, context="initial_proposal")

        if session.phase == "REFINEMENT":
            if proposal:
                # For formula parsing failures, return a focused warning instead of re-rendering full proposal.
                formula_requested = self._extract_formula_from_prompt(prompt) is not None
                proposal_changed = bool((session.extraction or {}).get("proposal_changed"))
                if formula_requested and proposal_changed:
                    formula_error = self._formula_error_reply(proposal)
                    if formula_error:
                        return formula_error
                return self._build_full_proposal_reply(session, proposal, context="refinement")
            else:
                return (
                    "I'm ready to refine your gradebook. "
                    "What would you like to change? You can adjust weights, add/remove categories or subcategories, or reorganize items."
                )

        if session.phase == "ACCEPTED":
            if text:
                is_mapping_action = bool(
                    self._looks_like_gradebook_refinement(text)
                    or self._looks_like_proposal_request(text)
                    or self._looks_like_affirmation(text)
                    or self._looks_like_upload_signal(text)
                    or re.search(r"\b(undo|redo)\b", text)
                    or re.search(r"\b(finalize|mapping|generate\s+mapping|manual\s+grade\s+item)\b", text)
                )
                if not is_mapping_action:
                    return self._unsupported_request_warning()
            return (
                "✓ Gradebook proposal accepted! "
                "I'm now mapping your course activities to the grade categories. "
                "Once complete, you'll review the mapping before I finalize everything in Moodle. "
                "If a category has no activity row, you can map an existing activity, remove the category, or add a manual grade item."
            )

        if session.phase == "COMPLETED":
            return (
                "✓ Gradebook finalized successfully! "
                "You can still edit it here by sending a change request (for example: 'change midterm to 30%'). "
                "After edits, regenerate mapping and finalize again to apply the override to Moodle."
            )

        return "Gradebook session updated. How can I help?"

    def _answer_question(self, session: GradebookSessionRecord, question_text: str, proposal: GradebookProposal | None) -> str:
        """Generate contextual answers to common gradebook questions."""
        q = question_text.lower()
        extraction = session.extraction or {}

        if proposal is not None and any(term in q for term in ("current effects", "what is the current effects", "what are the current effects", "effects we are using")):
            effects_block = self._format_effects(proposal, max_display=999)
            if effects_block:
                return f"Current effects (newest first):{effects_block}"
            return "No effects are active right now."

        if "syllabus" in q and any(term in q for term in ("do you have", "did you find", "found", "have one")):
            has_syllabus = bool(extraction.get("has_syllabus"))
            sources = extraction.get("syllabus_sources") or []
            if has_syllabus:
                if sources:
                    listed = ", ".join(str(s) for s in sources[:4])
                    return f"Yes. I found syllabus-like context in: {listed}."
                return "Yes. I found syllabus-like context from your uploaded or indexed course materials."
            return "No. I do not see a syllabus yet. Please upload one, place it in section 0, or paste the grading breakdown here."

        if any(term in q for term in ("what resource", "which resource", "what did you use", "what source")):
            sources = extraction.get("syllabus_sources") or []
            if sources:
                listed = "\n".join(f"- {s}" for s in sources[:8])
                return f"I used these syllabus/context resources:\n{listed}"
            visible = [r.name for r in session.moodle_resources if getattr(r, "name", None)]
            if visible:
                listed = "\n".join(f"- {s}" for s in visible[:8])
                return f"I did not detect a clear syllabus file. Current visible resources are:\n{listed}"
            return "I do not see any indexed syllabus resources yet."

        if any(term in q for term in ("total number of activity", "how many activit", "number of activit", "total activity")):
            total = len(session.course_activities or [])
            return f"I can see {total} Moodle activit{'y' if total == 1 else 'ies'} in this course."

        # Questions about categories
        if any(word in q for word in ("what is", "what's", "difference between", "distinguish")):
            if "assignment" in q and "lab" in q:
                return "**Assignments** are usually individual/group problem sets or exercises. **Labs** are hands-on practical work (coding, experiments, etc.). They're similar, so you could combine them if your course treats them the same way."
            if "quiz" in q and "exam" in q:
                return "**Quizzes** are shorter, more frequent assessments. **Exams** are comprehensive, high-stakes assessments. Both test knowledge, but exams typically count more toward the final grade."
            if "midterm" in q and "final" in q:
                return "**Midterm** is a major exam at the course's midpoint. **Final exam** is at the end. Both assess overall understanding, but the final is usually cumulative."
            if "participation" in q and ("discussion" in q or "forum" in q):
                return "**Participation** is engagement activity (class involvement, attendance). **Discussion** is online interaction in forums/chats. They overlap; combine them if your course doesn't distinguish between them."

        if any(term in q for term in ("grade item", "grade items", "activity and grade item", "grade item and activity", "activities and grade items", "activities", "activity")):
            listing_options = self._parse_listing_request_options(q)
            if listing_options:
                return self._format_activity_and_grade_item_summary(session, proposal, **listing_options)
            return self._format_activity_and_grade_item_summary(session, proposal)

        # Questions about weights
        if "weight" in q or "percentage" in q or "how much" in q:
            if "assignment" in q:
                return "The weight for assignments depends on your course design. Typical ranges: 20-40% for introductory courses, 30-50% for upper-level courses. How much do you want assignments to count?"
            if "exam" in q or "final" in q:
                return "Typical exam weights: midterm 15-25%, final exam 20-30%, depending on course emphasis. Comprehensive courses often weight the final higher (30-40%)."
            if "participation" in q:
                return "Participation/discussion weight varies: 5-15% for low-engagement courses, 15-30% for discussion-heavy courses. How much do you value engagement?"

        # Questions about adding/removing categories
        if ("add" in q or "remove" in q or "delete" in q) and ("categor" in q or "item" in q):
            return "You can add or remove categories anytime by describing them. Say 'Add Quizzes' to create it at 0%, 'Add a Projects category with 15%' to create it with weight, or 'Remove the Labs category and redistribute to Assignments.'"

        # Questions about uploads
        if "upload" in q or "file" in q:
            return "You can upload your syllabus or grading policy as a PDF or Word document. I'll extract the grading structure and use it to build your gradebook proposal."

        # Questions about aggregation/weighting method
        if any(term in q for term in ("aggregation", "aggregat", "method", "weight", "weighting system")):
            if any(term in q for term in ("current", "active", "currently", "now", "confirm")) and proposal is not None:
                name = self._get_aggregation_method_name(proposal.aggregation_method)
                return f"Current aggregation method is **{name}** (code {proposal.aggregation_method})."
            if any(term in q for term in ("what", "which", "difference", "list", "available", "support")):
                return self._aggregation_methods_help_text()
            if "recommend" in q or "suggest" in q or "which.*best" in q:
                return (
                    "For most courses, **Weighted mean (10)** is recommended as it's the standard grading method "
                    "where each category contributes according to its assigned weight. "
                    "Use **Natural (13)** if you want Moodle's default behavior, or **Mean with extra credits (12)** if your course offers extra credit opportunities."
                )

        # Questions about formulas
        if any(term in q for term in ("formula", "equation", "calculate", "computation", "do you support.*formula")):
            if "support" in q or "can.*do" in q or "help" in q or "how" in q:
                return self._formula_help_text()
            if "example" in q:
                return (
                    "**Formula Examples:**\n"
                    "- =average([[hw1]],[[hw2]],[[hw3]]) — Average of three homeworks\n"
                    "- =([[midterm]]*0.4)+([[final]]*0.6) — Weighted average: 40% midterm, 60% final\n"
                    "- =[[quiz1]]+[[quiz2]]*0.5 — Quiz 1 full, Quiz 2 half weight\n"
                    "- =([[lab]]*0.5)+([[project]]*0.5) — Split between lab and project\n\n"
                    "Tell me the formula you'd like and which category it should apply to."
                )

        # Questions about Moodle mapping
        if "mapp" in q or "activity" in q:
            return "Once we finalize your gradebook structure, I'll automatically map your Moodle course activities (assignments, quizzes, etc.) to the categories. You can review and adjust before finalizing."

        # Questions about drop/keep settings
        if any(term in q for term in ("drop lowest", "drop the lowest", "keep highest", "keep best", "keep top")):
            return (
                "You can configure per-category grade rules:\n"
                "- **Drop lowest N**: e.g. 'drop the lowest 2 from Assignments' — ignores the N worst grades\n"
                "- **Keep highest N**: e.g. 'keep the best 3 from Labs' — only counts the top N grades\n"
                "These are mutually exclusive per category."
            )

        # Questions about extra credit
        if "extra credit" in q:
            return (
                "To mark a category as extra credit, say something like:\n"
                "'Assignments count as extra credit' or 'extra credit for Labs'\n"
                "Extra credit grades can push the final grade above 100%."
            )

        # Questions about passing grade
        if any(term in q for term in ("pass", "passing grade", "grade to pass", "minimum to pass")):
            return (
                "You can set a passing threshold per category. For example:\n"
                "- 'Passing grade for Labs is 60'\n"
                "- 'Pass Midterm at 50'\n"
                "Students below this threshold will be marked as failing that category in Moodle."
            )

        # Questions about empty/missing grades
        if any(term in q for term in ("empty grade", "missing grade", "ungraded", "exclude empty", "include empty")):
            return (
                "By default, empty (ungraded) items are **excluded** from the category average. "
                "To change this: 'include empty grades for Assignments' or 'exclude empty grades for Labs'."
            )

        # Questions about outcome aggregation
        if any(term in q for term in ("outcome", "aggregate outcomes")):
            return (
                "You can control outcome aggregation per category:\n"
                "- 'Include outcomes for Labs'\n"
                "- 'Exclude outcomes for Midterm'"
            )

        # Questions about grade max / points
        if any(term in q for term in ("grade max", "maximum grade", "out of", "total points", "max points")):
            return (
                "Each category total defaults to 100 points. To change it:\n"
                "- 'Minimum grade for Assignments is 0'\n"
                "- 'Max grade for Assignments is 150'\n"
                "- 'Labs out of 50'\n"
                "- 'Set maximum for Midterm to 200'"
            )

        # Questions about hiding categories
        if any(term in q for term in ("hide", "hidden", "visible", "show category")):
            return (
                "You can hide or show categories from students:\n"
                "- 'Hide the Midterm category' — students won't see it until revealed\n"
                "- 'Hide Assignments until 2026-09-20' — hidden until that date\n"
                "- 'Show the Assignments category' — makes it visible again"
            )

        # Questions about locking
        if any(term in q for term in ("lock", "locked", "prevent override")):
            return (
                "Locking prevents manual grade overrides in a category:\n"
                "- 'Lock the Final Exam category' — prevents overrides\n"
                "- 'Lock Labs until 2026-11-15' — schedules lock time\n"
                "- 'Unlock Assignments' — allows changes again"
            )

        # Questions about display format / decimals
        if any(term in q for term in ("display", "show as", "format", "letter grade", "letter grade", "decimal")):
            return (
                "You can control how grades are displayed per category:\n"
                "- **Format**: 'Show Assignments as percentage', 'display Labs as letter', 'show Midterm as real'\n"
                "- **Decimals**: 'Use 2 decimal places for Assignments', 'show 0 decimals for Labs'\n"
                "Available formats: Default, Real (numeric), Percentage, Letter, Real+Percentage, Real+Letter"
            )

        # Default: no specific answer
        return ""

    def _format_categories(self, proposal: GradebookProposal, session: Optional[GradebookSessionRecord] = None) -> str:
        """Format categories as markdown bullets: parent -> subcategory -> item."""
        course_activities = list((session.course_activities if session else None) or [])
        content_mapping = session.content_mapping if session else None
        mapping_rows: List[dict] = (
            list(content_mapping.get("graded_activities") or [])
            if isinstance(content_mapping, dict)
            else []
        )

        def _row_category(row: dict) -> str:
            return str(
                row.get("confirmed_category")
                or row.get("category")
                or row.get("suggested_category")
                or ""
            ).strip()

        def _row_subcategory(row: dict) -> str:
            return str(
                row.get("confirmed_subcategory")
                or row.get("suggested_subcategory")
                or row.get("subcategory")
                or ""
            ).strip()

        proposal_tracked_names = _proposal_tracked_item_names(proposal)

        # Build mapping row index: parent_key -> sub_key -> [rows]
        rows_by_parent_sub: Dict[str, Dict[str, List[dict]]] = {}
        for row in mapping_rows:
            p = _row_category(row)
            if not p or p in {"__not_graded__", "__uncategorized__"}:
                continue
            s = _row_subcategory(row)
            rows_by_parent_sub.setdefault(p.lower(), {}).setdefault(s.lower(), []).append(row)

        mapped_parent_keys = set(rows_by_parent_sub.keys())

        def _has_nondefault_settings(category: GradebookCategory) -> bool:
            return bool(
                getattr(category, "extra_credit", False)
                or getattr(category, "hidden", False)
                or getattr(category, "locked", False)
                or getattr(category, "drop_lowest", 0) > 0
                or getattr(category, "keep_highest", 0) > 0
                or not getattr(category, "aggregate_only_graded", True)
                or bool(getattr(category, "aggregate_outcomes", False))
                or (getattr(category, "grade_min", None) is not None)
                or float(getattr(category, "grade_max", 100.0) or 100.0) != 100.0
                or (getattr(category, "grade_pass", None) is not None)
                or int(getattr(category, "display_type", 0) or 0) != 0
                or int(getattr(category, "decimals", -1) or -1) >= 0
                or bool(getattr(category, "calculation_formula", None))
            )

        def _is_renderable_category(category: GradebookCategory) -> bool:
            has_items = bool(category.items)
            has_subcategories = bool(category.subcategories)
            has_weight = abs(float(getattr(category, "weight", 0.0) or 0.0)) > 0.0001
            has_mapping = str(getattr(category, "name", "")).strip().lower() in mapped_parent_keys
            return has_items or has_subcategories or has_weight or has_mapping or _has_nondefault_settings(category)

        def _item_mapping(item_name: str) -> tuple[str, str]:
            """Return (category, subcategory) mapped for an activity/item name, if available."""
            name_key = str(item_name or "").strip().lower()
            if not name_key:
                return "", ""
            for row in mapping_rows:
                row_name = str(row.get("activity_name") or "").strip().lower()
                if row_name != name_key:
                    continue
                row_cat = _row_category(row)
                row_sub = _row_subcategory(row)
                return row_cat, row_sub
            return "", ""

        lines: List[str] = []
        categories = [category for category in (proposal.categories or []) if _is_renderable_category(category)]
        not_graded_items = [
            str(name).strip()
            for name in (getattr(proposal, "not_graded_items", None) or [])
            if str(name).strip()
        ]
        not_graded_keys = {name.lower() for name in not_graded_items}
        uncategorized_rows: List[dict] = []
        for row in mapping_rows:
            mapped = _row_category(row).lower()
            if mapped in {"__not_graded__", "not graded"} or bool(row.get("not_graded")):
                activity_name = str(row.get("activity_name") or "").strip()
                if activity_name:
                    not_graded_keys.add(activity_name.lower())
                continue
            activity_name = str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
            if activity_name and activity_name.lower() in proposal_tracked_names:
                continue
            if mapped in {"__uncategorized__", ""}:
                uncategorized_rows.append(row)

        n_cats = len(categories)
        has_unassigned = bool(uncategorized_rows)
        has_not_graded_footer = bool(not_graded_items)

        for cat_idx, category in enumerate(categories):
            is_last_cat = cat_idx == n_cats - 1 and not has_unassigned and not has_not_graded_footer
            cat_branch = "  └── " if is_last_cat else "  ├── "
            cat_cont   = "      " if is_last_cat else "  │   "

            parent_key = str(category.name).strip().lower()
            proposal_sub_item_keys = {
                str(item).strip().lower()
                for sub in (category.subcategories or [])
                for item in (sub.items or [])
                if str(item).strip()
            }

            def _subcategory_item_names(sub) -> List[str]:
                names: List[str] = []
                seen: set[str] = set()
                for item in (sub.items or []):
                    name = str(item).strip()
                    if name and name.lower() not in not_graded_keys and name.lower() not in seen:
                        seen.add(name.lower())
                        names.append(name)
                sub_key = str(sub.name).strip().lower()
                for row in rows_by_parent_sub.get(parent_key, {}).get(sub_key, []):
                    name = str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
                    if name and name.lower() not in not_graded_keys and name.lower() not in seen:
                        seen.add(name.lower())
                        names.append(name)
                return names

            def _direct_category_item_names() -> List[str]:
                placed_in_subs = {
                    name.lower()
                    for sub in (category.subcategories or [])
                    for name in _subcategory_item_names(sub)
                }
                names: List[str] = []
                seen: set[str] = set()
                for item in (category.items or []):
                    name = str(item).strip()
                    if not name or name.lower() in not_graded_keys:
                        continue
                    if name.lower() in proposal_sub_item_keys:
                        continue
                    if name.lower() in placed_in_subs:
                        continue
                    row_cat, row_sub = _item_mapping(name)
                    if row_sub and row_cat.lower() == parent_key:
                        continue
                    if name.lower() not in seen:
                        seen.add(name.lower())
                        names.append(name)
                return names

            weight = category.weight
            sub_item_count = sum(
                len(_subcategory_item_names(sub))
                for sub in (category.subcategories or [])
            )
            item_count = len(_direct_category_item_names()) + sub_item_count

            badges = ""
            if getattr(category, "extra_credit", False):
                badges += " ★ extra credit"
            if getattr(category, "hidden", False):
                badges += " 🔒 hidden"
            if getattr(category, "locked", False):
                badges += " 🔐 locked"

            settings: List[str] = []
            drop_lowest = getattr(category, "drop_lowest", 0)
            keep_highest = getattr(category, "keep_highest", 0)
            aggregate_only_graded = getattr(category, "aggregate_only_graded", True)
            aggregate_outcomes = getattr(category, "aggregate_outcomes", False)
            effective_children = len(getattr(category, "subcategories", None) or []) or len(category.items or [])

            if drop_lowest > 0:
                if effective_children > 0 and drop_lowest >= effective_children:
                    settings.append(f"drop lowest {drop_lowest} (no effect with {effective_children} children)")
                else:
                    settings.append(f"drop lowest {drop_lowest}")
            if keep_highest > 0:
                if effective_children > 0 and keep_highest >= effective_children:
                    settings.append(f"keep top {keep_highest} (no effect with {effective_children} children)")
                else:
                    settings.append(f"keep top {keep_highest}")
            if not aggregate_only_graded:
                settings.append("include empty grades")
            if aggregate_outcomes:
                settings.append("include outcomes")

            grade_min = getattr(category, "grade_min", None)
            grade_max = getattr(category, "grade_max", 100.0)
            grade_pass = getattr(category, "grade_pass", None)
            hidden_until = getattr(category, "hidden_until", None)
            lock_time = getattr(category, "lock_time", None)
            display_type = getattr(category, "display_type", 0)
            decimals = getattr(category, "decimals", -1)

            if grade_min is not None:
                settings.append(f"min {grade_min:.0f} pts")
            if grade_max != 100.0:
                settings.append(f"max {grade_max:.0f} pts")
            if grade_pass is not None:
                settings.append(f"pass ≥ {grade_pass:.0f}")
            if hidden_until:
                hu_label = datetime.fromtimestamp(int(hidden_until), tz=timezone.utc).strftime("%Y-%m-%d")
                settings.append(f"hidden until {hu_label}")
            if lock_time:
                lt_label = datetime.fromtimestamp(int(lock_time), tz=timezone.utc).strftime("%Y-%m-%d")
                settings.append(f"lock at {lt_label}")
            if display_type != 0:
                settings.append(f"display: {GRADE_DISPLAY_TYPE_NAMES.get(display_type, str(display_type))}")
            if decimals >= 0:
                settings.append(f"{decimals} decimal{'s' if decimals != 1 else ''}")

            formula = getattr(category, "calculation_formula", None)
            formula_badge = " 📐" if formula else ""
            settings_inline = f" [{'; '.join(settings)}]" if settings else ""

            lines.append(
                f"{cat_branch}**{category.name}** ({weight:.1f}%){badges}{formula_badge}{settings_inline}: "
                f"{item_count} item{'s' if item_count != 1 else ''}"
            )

            item_weights = getattr(category, "item_weights", None) or {}

            # Build children: subcategories first, then direct items.
            direct_items = _direct_category_item_names()
            children: list = [
                ("sub", sub) for sub in (category.subcategories or [])
            ] + [
                ("item", item) for item in direct_items
            ]
            n_children = len(children)
            for ci, (kind, child) in enumerate(children):
                is_last_ch = ci == n_children - 1
                ch_branch = cat_cont + ("└── " if is_last_ch else "├── ")
                ch_cont   = cat_cont + ("    " if is_last_ch else "│   ")

                if kind == "sub":
                    sub_weight = float(getattr(child, "weight", 0.0) or 0.0)
                    lines.append(f"{ch_branch}**{child.name}** ({sub_weight:.1f}%)")
                    sub_weights = getattr(child, "item_weights", None) or {}
                    sub_items = _subcategory_item_names(child)
                    n_sub = len(sub_items)
                    for si, item_name in enumerate(sub_items):
                        is_last_sub = si == n_sub - 1
                        si_branch = ch_cont + ("└── " if is_last_sub else "├── ")
                        w_label = f" ({sub_weights[item_name]:.1f}%)" if item_name in sub_weights else ""
                        item_kind = (
                            "activity"
                            if self._is_moodle_activity_item(item_name, course_activities, mapping_rows)
                            else "grade item"
                        )
                        item_context = f"[{item_kind}] [{category.name} > {child.name}]"
                        lines.append(f"{si_branch}{item_context} {item_name}{w_label}")
                else:
                    item_name = str(child).strip()
                    w_label = f" ({item_weights[item_name]:.1f}%)" if item_name in item_weights else ""
                    item_kind = (
                        "activity"
                        if self._is_moodle_activity_item(item_name, course_activities, mapping_rows)
                        else "grade item"
                    )
                    item_context = f"[{item_kind}] [{category.name}]"
                    lines.append(f"{ch_branch}{item_context} {item_name}{w_label}")

        if uncategorized_rows:
            lines.append("  └── **Unassigned**")
            n_un = len(uncategorized_rows)
            for ri, row in enumerate(uncategorized_rows):
                activity_name = str(row.get("activity_name") or "Unnamed activity")
                branch = "      └── " if ri == n_un - 1 and not has_not_graded_footer else "      ├── "
                lines.append(f"{branch}{activity_name}")

        if not_graded_items:
            lines.append("  └── **Not graded activities**")
            n_ng = len(not_graded_items)
            for ni, item_name in enumerate(not_graded_items):
                branch = "      └── " if ni == n_ng - 1 else "      ├── "
                lines.append(f"{branch}{item_name}")

        if not lines:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _normalize_mapping_label(value: str) -> str:
        return str(value or "").strip().lower()

    def _format_mapping_sync_status(self, session: GradebookSessionRecord, proposal: GradebookProposal) -> str:
        mapping = session.content_mapping if isinstance(session.content_mapping, dict) else {}
        rows = list(mapping.get("graded_activities") or [])
        if not rows:
            return (
                "\n\n**Mapping Sync:**"
                "\n- Mapping not generated yet. Say 'Accept proposal and generate mapping' to build rows."
            )

        proposal_categories = {
            self._normalize_mapping_label(category.name): category.name
            for category in (proposal.categories or [])
            if str(category.name or "").strip()
        }

        assigned_rows = 0
        not_graded_rows = 0
        missing_category_rows = 0
        missing_cmid_rows = 0
        stale_subcategory_rows = 0
        category_counts: Dict[str, int] = {}
        subcategory_counts: Dict[str, int] = {}
        for row in rows:
            mapped_name = str(row.get("confirmed_category") or row.get("suggested_category") or "").strip()
            mapped_key = self._normalize_mapping_label(mapped_name)
            if row.get("moodle_cmid") in (None, "", 0):
                missing_cmid_rows += 1
            if not mapped_key or mapped_key in {"__not_graded__", "__uncategorized__"}:
                if mapped_key in {"__not_graded__", "not graded"} or bool(row.get("not_graded")):
                    not_graded_rows += 1
                continue
            assigned_rows += 1
            if mapped_key not in proposal_categories:
                missing_category_rows += 1
                continue
            category_counts[proposal_categories[mapped_key]] = category_counts.get(proposal_categories[mapped_key], 0) + 1
            sub_name = str(
                row.get("confirmed_subcategory")
                or row.get("suggested_subcategory")
                or row.get("subcategory")
                or ""
            ).strip()
            if sub_name:
                sub_key = f"{proposal_categories[mapped_key]}/{sub_name}"
                subcategory_counts[sub_key] = subcategory_counts.get(sub_key, 0) + 1

        validation_errors = list(mapping.get("validation_errors") or [])
        for issue in validation_errors:
            if issue.get("missing_subcategory"):
                stale_subcategory_rows += 1

        lines = ["", "", "**Mapping Sync:**"]
        lines.append(f"- Mapped rows: {assigned_rows}/{len(rows)}")
        if not_graded_rows > 0:
            lines.append(f"- Not graded rows: {not_graded_rows}")
        lines.append(f"- Rows without Moodle CMID (manual-only rows): {missing_cmid_rows}")
        if category_counts:
            cat_parts = [f"{name}: {count}" for name, count in sorted(category_counts.items())]
            lines.append(f"- Rows by category: {', '.join(cat_parts)}")
        if subcategory_counts:
            sub_parts = [f"{name}: {count}" for name, count in sorted(subcategory_counts.items())]
            lines.append(f"- Rows by subcategory: {', '.join(sub_parts)}")
        if missing_category_rows > 0:
            lines.append(f"- Warning: {missing_category_rows} row(s) reference categories not present in current proposal.")
        if stale_subcategory_rows > 0:
            lines.append(
                f"- Warning: {stale_subcategory_rows} row(s) reference subcategories not present under their parent category."
            )

        return "\n".join(lines)

    def _reply_for_proposal_view(
        self,
        session: GradebookSessionRecord,
        proposal: GradebookProposal | None,
        view: str,
    ) -> str:
        if view == "mapping_sync":
            sync_proposal = proposal or GradebookProposal(categories=[])
            return (
                "Here is the current mapping sync status:"
                f"{self._format_mapping_sync_status(session, sync_proposal)}"
            )

        if view == "mapping_rows":
            return self._format_mapping_rows(session, proposal)

        if proposal is None:
            return (
                "No proposal is available yet. "
                "Upload a syllabus or ask for a proposal first."
            )

        if view == "markdown_tree":
            categories_text = self._format_categories(proposal, session)
            if not categories_text:
                return "No category hierarchy to display yet."
            total_weight = sum(cat.weight for cat in proposal.categories)
            return (
                f"**Proposal hierarchy:**\n\n"
                f"{categories_text}\n\n"
                f"**Total: {total_weight:.1f}%**"
            )

        if view == "full_proposal":
            if session.phase in {"ACCEPTED", "COMPLETED"}:
                context = "review"
            elif session.phase == "PROPOSAL":
                context = "initial_proposal"
            else:
                context = "refinement"
            return self._build_full_proposal_reply(session, proposal, context=context)

        return "I could not render that view."

    def _build_full_proposal_reply(
        self,
        session: GradebookSessionRecord,
        proposal: GradebookProposal,
        *,
        context: str = "refinement",
    ) -> str:
        categories_text = self._format_categories(proposal, session)
        total_weight = sum(cat.weight for cat in proposal.categories)
        effects_text = self._format_effects(proposal)
        notes_text = self._format_notes(session, proposal)
        item_weight_warning_text = self._format_item_weight_warning(proposal)
        mapping_sync_text = self._format_mapping_sync_status(session, proposal)
        findings_text = self._format_findings(session) if context == "initial_proposal" else ""
        aggregation_name = self._get_aggregation_method_name(proposal.aggregation_method)

        proposal_errors = validate_proposal_weights(proposal)
        has_weight_error = any(err.get("path") == ["proposal"] for err in proposal_errors)
        weight_warnings = check_weight_warnings(proposal)

        if context == "initial_proposal":
            intro = (
                "Here is the gradebook structure I built based on your materials:"
                if has_weight_error
                else "Here is the gradebook structure based on what I found:"
            )
        elif context == "review":
            intro = "Current proposal:"
        else:
            intro = "Updated proposal:"

        body = (
            f"{intro}\n\n"
            f"{findings_text}{categories_text}\n**Total: {total_weight:.1f}%**"
            f"{mapping_sync_text}{effects_text}{notes_text}{item_weight_warning_text}"
        )

        if has_weight_error:
            if context == "initial_proposal":
                return (
                    f"{body}\n\n"
                    "⚠ The total weight is not 100% yet. Please adjust the weights so they add up to 100% before we continue."
                )
            return (
                f"{body}\n\n"
                f"**Grade Aggregation Method**: {aggregation_name}\n\n"
                "⚠ Total weight is still not 100%. Please adjust to proceed."
            )

        weight_msg = f"\n{weight_warnings[0]}" if weight_warnings else ""
        aggregation_block = f"\n\n**Grade Aggregation Method**: {aggregation_name}"
        if context == "initial_proposal":
            aggregation_block += (
                f"\n(I'll use '{aggregation_name}' when creating your gradebook. "
                "If you prefer a different method, let me know.)"
            )

        footer = ""
        if context == "initial_proposal":
            footer = (
                "\n\nDoes this look right? If you'd like to adjust any weights, categories, "
                "or the grade aggregation method, let me know and I'll refine it."
            )
        elif context == "refinement":
            footer = (
                "\n\nWhat else would you like to adjust? I can modify weights, add/remove "
                "categories or subcategories, or rename items."
            )

        return f"{body}{weight_msg}{aggregation_block}{footer}"

    def _format_mapping_rows(
        self,
        session: GradebookSessionRecord,
        proposal: GradebookProposal | None,
    ) -> str:
        mapping = session.content_mapping if isinstance(session.content_mapping, dict) else {}
        rows = list(mapping.get("graded_activities") or [])
        if not rows:
            return (
                "**Mapping rows:** none yet.\n"
                "Say **Accept proposal and generate mapping** to build rows before finalize."
            )

        lines = [
            "**Mapping rows** (verify cmid / category / subcategory before finalize):",
            "",
        ]
        for index, row in enumerate(rows, start=1):
            activity_name = str(row.get("activity_name") or "Unnamed activity").strip()
            cmid = row.get("moodle_cmid")
            cmid_label = str(cmid) if cmid not in (None, "", 0) else "manual"
            category = str(row.get("confirmed_category") or row.get("suggested_category") or "—").strip()
            subcategory = str(
                row.get("confirmed_subcategory")
                or row.get("suggested_subcategory")
                or row.get("subcategory")
                or ""
            ).strip() or "—"
            extras: List[str] = []
            grade_item_id = row.get("grade_item_id")
            if grade_item_id not in (None, "", 0):
                extras.append(f"grade_item_id={grade_item_id}")
            itemtype = str(row.get("itemtype") or "").strip()
            if itemtype:
                extras.append(itemtype)
            extra_suffix = f" | {' | '.join(extras)}" if extras else ""
            lines.append(
                f"{index}. **{activity_name}** — cmid {cmid_label} | {category} > {subcategory}{extra_suffix}"
            )

        validation_errors = list(mapping.get("validation_errors") or [])
        if validation_errors:
            lines.append("")
            lines.append(f"**Validation issues:** {len(validation_errors)}")
            for issue in validation_errors[:8]:
                activity_name = str(issue.get("activity_name") or "Row").strip()
                if issue.get("missing_subcategory"):
                    parent = str(issue.get("parent_category") or "category").strip()
                    missing = str(issue.get("missing_subcategory") or "subcategory").strip()
                    lines.append(f"- {activity_name}: missing subcategory '{missing}' under {parent}")
                else:
                    lines.append(f"- {activity_name}: {issue}")

        return "\n".join(lines)

    def _generate_effects(self, proposal: GradebookProposal, previous: GradebookProposal | None = None) -> List[str]:
        """Generate a list of human-readable effects from the current proposal state."""
        # Prefer explicit effect log entries (chronological), newest first.
        notes = list(proposal.notes or [])
        logged = [n[len("Effect:"):].strip() for n in notes if n.startswith("Effect:")]
        if not logged:
            return []

        # Deduplicate by topic key (newest wins) so same-topic overrides don't show duplicates.
        seen_topics = set()
        ordered = []
        for item in reversed(logged):
            topic = ProposalGenerator._effect_topic_key(item)
            if topic in seen_topics:
                continue
            seen_topics.add(topic)
            ordered.append(item)
        return ordered

    @staticmethod
    def _aggregation_methods_help_text() -> str:
        return (
            "**Grade Aggregation Methods** determine how Moodle calculates final grades:\n"
            "- **Mean of grades (0)**: Simple average of all grades\n"
            "- **Weighted mean of grades (10)**: Sum of (grade × weight) / sum of weights\n"
            "- **Simple weighted mean of grades (11)**: Weighted calculation with simplified handling\n"
            "- **Mean of grades (with extra credits) (12)**: Supports extra-credit grades above 100%\n"
            "- **Natural (13)**: Moodle default aggregation\n\n"
            "Tell me which one you want to use and I'll apply it."
        )

    @staticmethod
    def _formula_help_text() -> str:
        return (
            "**Excel-Style Formulas** allow you to define custom grade calculations using item references.\n"
            "**Item references:** wrap IDs in double square brackets, e.g. `[[hw1]]`. Legacy `[item]` input is also accepted.\n\n"
            "**Examples:**\n"
            "- Simple average: `=average([[hw1]],[[hw2]],[[hw3]])`\n"
            "- Weighted calculation: `=([[midterm]]*0.3)+([[final]]*0.7)`\n"
            "- Nested functions: `=round(average([[q1]],[[q2]]),2)`\n"
            "- Conditional: `=if([[bonus]]>0, [[score]]+[[bonus]], [[score]])`\n\n"
            "**Supported operators:** `+` (addition), `-` (subtraction), `*` (multiplication), `/` (division)\n"
            "Use parentheses `()` for grouping. Use comma `,` between function arguments.\n\n"
            "Tell me the formula you'd like to use and which category it applies to."
        )

    @staticmethod
    def _formula_ignored_notes(proposal: GradebookProposal | None) -> List[str]:
        if proposal is None:
            return []
        notes = proposal.notes or []
        return [str(n) for n in notes if str(n).startswith("Formula ignored:")]

    @staticmethod
    def _formula_unresolved_notes(
        proposal: GradebookProposal | None,
        target_category: str | None = None,
    ) -> List[str]:
        if proposal is None:
            return []
        notes: List[str] = []
        target_norm = (target_category or "").strip().lower()
        for category in proposal.categories:
            if target_norm and str(getattr(category, "name", "")).strip().lower() != target_norm:
                continue
            unresolved = list(getattr(category, "formula_unresolved_refs", []) or [])
            if not unresolved:
                continue
            refs = ", ".join(f"[{ref}]" for ref in unresolved)
            notes.append(f"{category.name}: unresolved formula refs {refs}")
        return notes

    @staticmethod
    def _latest_formula_effect_target(proposal: GradebookProposal | None) -> str | None:
        if proposal is None:
            return None
        notes = list(proposal.notes or [])
        for note in reversed(notes):
            text = str(note)
            if not text.startswith("Effect:"):
                continue
            effect = text[len("Effect:"):].strip()

            # Effect: Applied formula to Assignments: =...
            applied = re.match(r"^Applied\s+formula\s+to\s+(.+?)(?::|$)", effect, flags=re.IGNORECASE)
            if applied:
                return applied.group(1).strip(" '")

            # Effect: Stored formula for 'Labs' (unresolved refs: ...).
            stored = re.match(r"^Stored\s+formula\s+for\s+'?(.+?)'?(?:\s*\(|:|$)", effect, flags=re.IGNORECASE)
            if stored:
                return stored.group(1).strip(" '")

            cleared = re.match(r"^Cleared\s+formula\s+from\s+(.+?)(?::|$)", effect, flags=re.IGNORECASE)
            if cleared:
                return cleared.group(1).strip(" '")

        return None

    def _formula_error_reply(self, proposal: GradebookProposal | None) -> str:
        errors = self._formula_ignored_notes(proposal)
        latest_target = self._latest_formula_effect_target(proposal)
        unresolved_notes = self._formula_unresolved_notes(proposal, target_category=latest_target)
        if not errors:
            if not unresolved_notes:
                return ""

        # When parsing failed this turn, focus on parser errors only and do not
        # include stale unresolved refs from prior formulas/categories.
        if errors:
            unresolved_notes = []

        combined: List[str] = []
        seen = set()
        for item in (errors[-2:] + unresolved_notes[-2:]):
            key = item.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)

        header = "I couldn't apply that formula yet."
        guidance = "Please review the issue and retry:"
        if not errors and unresolved_notes:
            header = "I saved the formula, but some references are still unresolved."
            guidance = "Please fix these references to fully apply it:"

        lines = [
            header,
            "",
            guidance,
        ]
        for err in combined:
            lines.append(f"- {err.replace('Formula ignored: ', '').strip()}")

        lines.extend([
            "",
            "Tips:",
            "- Use item references like [[hw1]] and start with '='.",
            "- Use comma ',' between function arguments (YorkU standard).",
            "",
            "Help resources:",
            "- [YorkU custom formula guide](https://lthelp.yorku.ca/gradebook/creating-a-custom-formula)",
            "- [Excel formula help](https://support.microsoft.com/excel)",
        ])
        return "\n".join(lines)

    def _format_effects(self, proposal: GradebookProposal, max_display: int = 6) -> str:
        """Format effects for display, showing recent ones with 'show all' if needed."""
        effects = self._generate_effects(proposal)
        if not effects:
            return ""

        lines = ["", "", "**Effects:**"]
        if len(effects) <= max_display:
            for effect in effects:
                lines.append(f"- {effect}")
        else:
            for effect in effects[:max_display]:
                lines.append(f"- {effect}")
            lines.append(f"- ... and {len(effects) - max_display} more effect(s)")

        return "\n".join(lines)

    def _format_notes(self, session: GradebookSessionRecord, proposal: GradebookProposal) -> str:
        issues = self.validate_weights(proposal)
        notes = [n for n in (proposal.notes or []) if not str(n).startswith("Effect:")]
        conflict_notes = self._syllabus_conflict_warnings(session, proposal)
        unresolved_notes = self._formula_unresolved_notes(proposal)
        if not issues and not notes and not conflict_notes and not unresolved_notes:
            return ""

        lines = ["", "", "**Checks:**"]
        for issue in issues:
            lines.append(f"- {issue}")
        # Deduplicate repeated warnings to keep the response concise.
        merged_notes = notes[-4:] + unresolved_notes[-2:]
        seen = set()
        deduped = []
        for note in merged_notes:
            key = str(note).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(note)
        for note in deduped:
            lines.append(f"- {note}")
        for note in conflict_notes:
            lines.append(f"- {note}")
        return "\n".join(lines)

    @staticmethod
    def _format_item_weight_warning(proposal: GradebookProposal) -> str:
        item_weight_notes = [
            str(n).strip()
            for n in (proposal.notes or [])
            if str(n).strip().lower().startswith("item weight check:")
        ]
        if not item_weight_notes:
            return ""

        lines = [
            "",
            "",
            "**Weight Update Warning:**",
            "- One or more requested item weight changes were not applied.",
        ]
        for note in item_weight_notes[-2:]:
            lines.append(f"- {note}")
        lines.append("- Existing valid item weights were kept unchanged.")
        return "\n".join(lines)

    def _syllabus_conflict_warnings(self, session: GradebookSessionRecord, proposal: GradebookProposal) -> List[str]:
        extraction = session.extraction or {}
        if not extraction.get("has_syllabus"):
            return []

        preview_texts = []
        for resource in session.moodle_resources or []:
            preview = (resource.content_preview or "").strip()
            if preview:
                preview_texts.append(preview)
        uploaded_text = str(extraction.get("uploaded_syllabus_text") or "").strip()
        if uploaded_text:
            preview_texts.append(uploaded_text)
        if not preview_texts:
            return []

        source_text = "\n".join(preview_texts)
        pattern = re.compile(
            r"\b(assignments?|labs?|mid\s*term|midterm|final(?:\s*exam)?|quizzes?|projects?|participation)\b[^\n%]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%",
            flags=re.IGNORECASE,
        )

        hinted: Dict[str, float] = {}
        for match in pattern.finditer(source_text):
            raw_name = match.group(1).lower().strip()
            val = float(match.group(2))
            canonical = self._canonical_category_name(raw_name)
            hinted[canonical] = val

        if not hinted:
            return []

        proposal_map = {self._canonical_category_name(c.name): float(c.weight) for c in proposal.categories}
        conflicts: List[str] = []
        for name, hinted_weight in hinted.items():
            if name in proposal_map:
                delta = abs(proposal_map[name] - hinted_weight)
                if delta >= 5.0:
                    conflicts.append(
                        f"Warning: syllabus hints {name} at {hinted_weight:.1f}%, but proposal uses {proposal_map[name]:.1f}%."
                    )
        return conflicts[:3]

    @staticmethod
    def _canonical_category_name(name: str) -> str:
        n = re.sub(r"\s+", " ", (name or "").strip().lower())
        if n in {"assignment", "assignments"}:
            return "Assignments"
        if n in {"lab", "labs"}:
            return "Labs"
        if n in {"midterm", "mid term"}:
            return "Midterm"
        if n in {"final", "final exam", "exam"}:
            return "Final Exam"
        if n in {"quiz", "quizzes"}:
            return "Quizzes"
        if n in {"project", "projects"}:
            return "Projects"
        if n in {"participation"}:
            return "Participation"
        return name.strip().title()

    def _format_findings(self, session: GradebookSessionRecord) -> str:
        extraction = session.extraction or {}
        if not isinstance(extraction, dict) or not extraction:
            return ""

        summary_parts = []
        assessment_types = extraction.get("assessment_types")
        if assessment_types:
            summary_parts.append(f"assessment types: {assessment_types}")
        grading_notes = extraction.get("grading_policy")
        if grading_notes:
            summary_parts.append(f"grading policy: {grading_notes}")

        if not summary_parts:
            return ""

        return "I detected the following from syllabus/materials: " + " | ".join(summary_parts) + "\n\n"

    def _format_activity_and_grade_item_summary(
        self,
        session: GradebookSessionRecord,
        proposal: GradebookProposal | None,
        include_activities: bool = True,
        include_grade_items: bool = True,
        show_all: bool = False,
    ) -> str:
        activities = list(session.course_activities or [])
        content_mapping = session.content_mapping if isinstance(session.content_mapping, dict) else {}
        mapping_rows: List[dict] = list(content_mapping.get("graded_activities") or [])
        moodle_activities = [
            activity for activity in activities if self._course_activity_is_moodle_activity(activity)
        ]

        lines = ["Here is the current activity and grade-item summary:"]
        hidden_activity_count = 0
        hidden_grade_item_count = 0

        if include_activities:
            lines.append("")
            lines.append("**Moodle activities:**")
            if moodle_activities:
                activity_limit = len(moodle_activities) if show_all else 8
                for activity in moodle_activities[:activity_limit]:
                    module = f" ({activity.module})" if getattr(activity, "module", None) else ""
                    lines.append(f"- {activity.name}{module}")

                if len(moodle_activities) > activity_limit:
                    hidden_activity_count = len(moodle_activities) - activity_limit
                    lines.append(f"- ... and {hidden_activity_count} more activity(ies)")
            else:
                lines.append("- No Moodle activities were found for this session.")

        if include_grade_items:
            lines.append("")
            lines.append("**Proposal grade items:**")
            if proposal and proposal.categories:
                for category in proposal.categories:
                    # Collect items from both parent category and subcategories
                    parent_items = list(category.items or [])
                    all_items = list(parent_items)
                    
                    # Add items from subcategories
                    for sub in (category.subcategories or []):
                        sub_items = list(sub.items or [])
                        for sub_item in sub_items:
                            # Prefix subcategory items with subcategory name
                            all_items.append(f"{sub_item} ({sub.name})")
                    
                    if not all_items:
                        lines.append(f"- {category.name}: no grade items yet")
                        continue

                    item_limit = len(all_items) if show_all else 5
                    rendered_items = []
                    for item in all_items[:item_limit]:
                        # Extract base item name for activity lookup (before subcategory marker)
                        base_item = str(item).split(" (")[0].strip() if " (" in str(item) else str(item)
                        label = (
                            "activity"
                            if self._is_moodle_activity_item(base_item, activities, mapping_rows)
                            else "manual"
                        )
                        rendered_items.append(f"{item} [{label}]")

                    if len(all_items) > item_limit:
                        hidden_grade_item_count += len(all_items) - item_limit
                        extra = f" ... and {len(all_items) - item_limit} more"
                    else:
                        extra = ""

                    lines.append(f"- {category.name}: {', '.join(rendered_items)}{extra}")
            else:
                lines.append("- No proposal is available yet.")

        not_graded_items = list(getattr(proposal, "not_graded_items", None) or []) if proposal else []
        if not_graded_items:
            lines.append("")
            lines.append("**Not graded activities:**")
            for item_name in not_graded_items:
                lines.append(f"- {item_name} [not graded]")

        if not show_all and (hidden_activity_count > 0 or hidden_grade_item_count > 0):
            lines.append("")
            hints = []
            if hidden_activity_count > 0:
                hints.append("'show all activities'")
            if hidden_grade_item_count > 0:
                hints.append("'show all grade items'")
            lines.append("Tip: use " + " or ".join(hints) + " for complete lists.")

        return "\n".join(lines)

    @staticmethod
    def _get_aggregation_method_name(method_code: int) -> str:
        """Convert aggregation method code to readable name."""
        methods = {
            0: "Mean of grades",
            10: "Weighted mean of grades",
            11: "Simple weighted mean of grades",
            12: "Mean of grades (with extra credits)",
            13: "Natural",
        }
        return methods.get(method_code, f"Unknown method ({method_code})")

    def validate_weights(self, proposal: GradebookProposal) -> List[str]:
        """Validate that proposal weights are reasonable"""
        issues = []
        for error in validate_proposal_weights(proposal):
            path = error.get("path") or []
            total_weight = float(error.get("total_weight", 0.0))
            if path == ["proposal"]:
                issues.append(f"Total weight is {total_weight:.1f}%, should be 100%")

        for category in proposal.categories:
            if category.weight < 0:
                issues.append(f"Category '{category.name}' has invalid weight {category.weight}")
            if category.weight > 100:
                issues.append(f"Category '{category.name}' weight {category.weight}% seems too high")

        return issues
