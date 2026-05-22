from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

from .schemas import (
    CourseActivity, GradebookCategory, GradebookSubcategory, GradebookProposal,
    GRADE_DISPLAY_TYPE_DEFAULT, GRADE_DISPLAY_TYPE_REAL, GRADE_DISPLAY_TYPE_PERCENTAGE,
    GRADE_DISPLAY_TYPE_LETTER, GRADE_DISPLAY_TYPE_REAL_PERCENTAGE, GRADE_DISPLAY_TYPE_REAL_LETTER,
    GRADE_DISPLAY_TYPE_LETTER_REAL, GRADE_DISPLAY_TYPE_PERCENTAGE_REAL,
)
from .formula_parser import FormulaParser
from .formula_resolver import FormulaResolver


def validate_proposal_weights(proposal: GradebookProposal) -> List[Dict[str, object]]:
    """Validate proposal weights for aggregation methods that require strict 100% total.
    
    - Methods 10, 11 (weighted means): MUST total exactly 100%
    - Methods 12 (mean+extra): MUST total 100% (excluding extra-credit categories)
    - Methods 0, 13: Allow any total (validation/warning handled elsewhere)
    """
    method = int(getattr(proposal, "aggregation_method", 13))
    # Only strict validation for weighted/mean methods
    if method not in {10, 11, 12}:
        return []

    categories = list(getattr(proposal, "categories", []) or [])
    if not categories:
        return []

    weighted_categories = categories
    if method == 12:
        weighted_categories = [cat for cat in categories if not bool(getattr(cat, "extra_credit", False))]

    total_weight = sum(float(getattr(cat, "weight", 0.0)) for cat in weighted_categories)
    errors: List[Dict[str, object]] = []
    if abs(total_weight - 100.0) > 0.01:
        errors.append(
            {
                "path": ["proposal"],
                "aggregation_method": method,
                "total_weight": total_weight,
                "expected": 100.0,
                "details": (
                    f"Weight sum for proposal categories is {total_weight:.1f}%, expected 100.0% "
                    f"for aggregation method {method}. Please adjust category weights to total 100%."
                ),
            }
        )

    return errors


class ProposalGenerator:
    DEFAULT_CATEGORIES = [
        ("Assignments", 25.0),
        ("Labs", 15.0),
        ("Midterm", 30.0),
        ("Final Exam", 30.0),
    ]
    CATEGORY_ALIASES = {
        "assignment": "Assignments",
        "assignments": "Assignments",
        "homework": "Assignments",
        "hw": "Assignments",
        "lab": "Labs",
        "labs": "Labs",
        "lap": "Labs",
        "midterm": "Midterm",
        "mid term": "Midterm",
        "exam": "Final Exam",
        "final": "Final Exam",
        "final exam": "Final Exam",
        "quiz": "Quizzes",
        "quizzes": "Quizzes",
        "quize": "Quizzes",
        "quizes": "Quizzes",
    }
    CATEGORY_ALIAS_PATTERNS = (
        (re.compile(r"\b(assign(?:ment)?s?|home\s*work|hw)\b", flags=re.IGNORECASE), "Assignments"),
        (re.compile(r"\b(quiz(?:zes)?|quize(?:s)?|test(?:s)?)\b", flags=re.IGNORECASE), "Quizzes"),
        (re.compile(r"\b(lab(?:s)?|lap(?:s)?|laboratory)\b", flags=re.IGNORECASE), "Labs"),
        (re.compile(r"\b(mid\s*term|midterm)\b", flags=re.IGNORECASE), "Midterm"),
        (re.compile(r"\b(final(?:\s*exam)?|exam)\b", flags=re.IGNORECASE), "Final Exam"),
    )

    def __init__(self) -> None:
        self._category_aliases = dict(self.CATEGORY_ALIASES)
        self._category_alias_patterns = list(self.CATEGORY_ALIAS_PATTERNS)
        self._load_aliases_from_env()

    def generate_initial(self, activities: List[CourseActivity]) -> GradebookProposal:
        categories = [GradebookCategory(name=name, weight=weight, items=[]) for name, weight in self.DEFAULT_CATEGORIES]
        by_name = {category.name: category for category in categories}

        def ensure_category(name: str) -> GradebookCategory:
            existing = by_name.get(name)
            if existing is not None:
                return existing

            # Inferred categories start at zero weight so we don't disturb the
            # default 100% baseline before the professor reviews the proposal.
            category = GradebookCategory(name=name, weight=0.0, items=[])
            categories.append(category)
            by_name[name] = category
            return category

        for activity in activities:
            activity_name = activity.name.lower()
            module_name = (activity.module or "").lower()

            if module_name == "quiz" or any(keyword in activity_name for keyword in ("quiz", "knowledge check", "test")):
                ensure_category("Quizzes").items.append(activity.name)
            elif "lab" in activity_name:
                categories[1].items.append(activity.name)
            elif "midterm" in activity_name:
                categories[2].items.append(activity.name)
            elif "final" in activity_name or "exam" in activity_name:
                categories[3].items.append(activity.name)
            else:
                categories[0].items.append(activity.name)

        return GradebookProposal(
            categories=categories,
            notes=[
                "Initial proposal is generated from available course context and Moodle activities.",
                "Professor can refine category names, weights, and mapping before acceptance.",
            ],
        )

    # Notes that are auto-generated (not user-visible descriptive text) get stripped each turn.
    _EPHEMERAL_NOTE_PREFIXES = (
        "weight check:",
        "auto-normalized",
        "removed category",
        "formula ignored:",
        "formula warning:",
        "formula was valid, but no target category was found.",
    )

    @staticmethod
    def _is_ephemeral_note(note: str) -> bool:
        low = note.lower()
        return any(low.startswith(prefix) for prefix in ProposalGenerator._EPHEMERAL_NOTE_PREFIXES)

    @staticmethod
    def _append_unique_note(proposal: GradebookProposal, note: str) -> None:
        notes = proposal.notes or []
        if note not in notes:
            notes.append(note)
        proposal.notes = notes

    @staticmethod
    def _effect_topic_key(effect: str) -> str:
        """Return a stable topic key so later effects override earlier ones on same topic."""
        text = (effect or "").strip()
        low = text.lower()

        m = re.match(r"^in\s+(.+?),\s*split\s+into\b", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"split:{label}"

        m = re.match(r"^keep\s+highest\s+\d+\s+from\s+(.+)$", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"keephigh:{label}"

        m = re.match(r"^drop\s+lowest\s+\d+\s+from\s+(.+)$", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"droplow:{label}"

        if low.startswith("aggregation method set to"):
            return "aggregation_method"

        m = re.match(r"^added\s+'?(.+?)'?\s+with\s+weight\s+\d+(?:\.\d+)?%", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '")
            return f"category_add:{label}"

        m = re.match(r"^(?:set|changed?|adjusted?|updated?)\s+(.+?)\s+to\s+\d+(?:\.\d+)?%", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '")
            return f"weight:{label}"

        # Formula effects on the same category should share one topic key.
        m = re.match(r"^(?:applied|cleared)\s+formula\s+(?:to|from)\s+(.+?)(?::|$)", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '\"")
            return f"formula:{label}"

        m = re.match(r"^stored\s+formula\s+for\s+(.+?)(?:\s*\(|:|$)", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '\"")
            return f"formula:{label}"

        m = re.match(r"^(.+?)\s+hidden\s+until\b", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"hiddenuntil:{label}"

        return low

    @staticmethod
    def _append_effect_note(proposal: GradebookProposal, effect: str) -> None:
        notes = proposal.notes or []
        clean_effect = effect.strip()
        entry = f"Effect: {clean_effect}"
        topic_key = ProposalGenerator._effect_topic_key(clean_effect)

        # Keep only the latest entry per topic (e.g., latest Labs split overrides older Labs split).
        filtered = []
        for n in notes:
            if not str(n).startswith("Effect:"):
                filtered.append(n)
                continue
            old_effect = str(n)[len("Effect:"):].strip()
            if ProposalGenerator._effect_topic_key(old_effect) == topic_key:
                continue
            filtered.append(n)

        if not filtered or filtered[-1] != entry:
            filtered.append(entry)
        proposal.notes = filtered

    def update_from_prompt(
        self,
        proposal: GradebookProposal,
        prompt: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> GradebookProposal:
        updated = GradebookProposal.parse_obj(proposal.model_dump())
        prompt_lower = prompt.lower()

        # Strip ephemeral per-turn notes so they don't accumulate.
        updated.notes = [n for n in (updated.notes or []) if not self._is_ephemeral_note(n)]

        # Detect split context early so weight extraction skips subcategory names.
        # Support both:
        # - "split ... into ..."
        # - "add subcategories: ..."
        is_split_prompt = bool(re.search(r"\b(?:split|divide)\b.+\binto\b|\badd\s+subcategories\b", prompt_lower))

        self._apply_removals(updated, prompt)

        weights = self._parse_weight_assignments(updated, prompt, skip_split_pieces=is_split_prompt)

        # Creation-style prompts that provide a full category set should replace categories,
        # not mutate previous defaults in-place.
        if self._should_rebuild_category_set(prompt, weights):
            updated.categories = self._rebuild_categories_from_weights(updated, weights)
            weights = {}

        # Support directives like "give remaining weight to midterm".
        remaining_target = self._parse_remaining_target(updated, prompt)
        if remaining_target:
            weights = self._apply_remaining_weight_directive(updated, weights, remaining_target)

        if weights:
            self._apply_weight_updates(updated, weights)

        self._apply_add_category_requests(updated, prompt, explicit_updates=weights)

        self._apply_directive_normalization(updated, prompt, explicit_updates=weights)

        # Apply per-category settings (drop/keep/extra credit/exclude empty)
        self._apply_category_settings(updated, prompt)

        # Parse and persist optional formula-driven grading requests.
        self._apply_formula_request(updated, prompt, course_activities=course_activities or [])

        # Parse clear/remove formula directives after apply, so replacement prompts work.
        self._apply_formula_clear_request(updated, prompt)

        # Persist aggregation method requests (e.g., "use weighted mean").
        self._apply_aggregation_method(updated, prompt)

        if is_split_prompt:
            self._apply_split_request(updated, prompt)

        self._post_update_checks(updated, prompt)

        return updated

    def _detect_formula_target_category(self, proposal: GradebookProposal, prompt: str, item_refs: List[str]) -> Optional[GradebookCategory]:
        """Best-effort target category detection for a formula prompt."""
        prompt_l = (prompt or "").lower()

        explicit = re.search(r"\b(?:set|use|apply)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+(?:as|formula|calculation)", prompt_l)
        if explicit:
            hint = explicit.group(1).strip()
            targets = self._resolve_category_targets(proposal, hint)
            if targets:
                return targets[0]

        for category in proposal.categories:
            if category.name.lower() in prompt_l:
                return category

        for ref in item_refs:
            ref_targets = self._resolve_category_targets(proposal, ref)
            if ref_targets:
                return ref_targets[0]

        final_targets = self._resolve_category_targets(proposal, "final")
        if final_targets:
            return final_targets[0]

        return proposal.categories[0] if proposal.categories else None

    def _apply_formula_request(
        self,
        proposal: GradebookProposal,
        prompt: str,
        course_activities: List[CourseActivity],
    ) -> None:
        """Extract, validate, and store a formula request in the proposal when present."""
        detected = FormulaParser.extract_formula_and_detect(prompt)
        if not detected:
            return

        raw_formula = str(detected.get("formula") or "").strip()
        if not raw_formula:
            return

        validation = FormulaParser.parse_formula(raw_formula)
        if not validation.is_valid:
            self._append_unique_note(
                proposal,
                f"Formula ignored: {validation.error_message}",
            )
            return

        target = self._detect_formula_target_category(
            proposal=proposal,
            prompt=prompt,
            item_refs=validation.item_references or [],
        )
        if target is None:
            self._append_unique_note(
                proposal,
                "Formula was valid, but no target category was found.",
            )
            return

        normalized = validation.normalized_formula or raw_formula.lstrip("=").strip()
        if course_activities:
            category_context_names = []
            for c in (proposal.categories or []):
                if getattr(c, "name", None):
                    category_context_names.append(c.name)
                for sub in (getattr(c, "subcategories", None) or []):
                    if getattr(sub, "name", None):
                        category_context_names.append(sub.name)
            resolved_formula, unresolved_refs, suggestions = FormulaResolver.resolve_formula(
                formula=f"={normalized}",
                activities=course_activities,
                category_names=category_context_names,
            )
        else:
            # Backward-compatible behavior: when activity context is unavailable,
            # keep normalized refs as-is and treat the formula as applied.
            resolved_formula = FormulaParser.moodle_compatible_formula(f"={normalized}")
            unresolved_refs = []
            suggestions = {}

        target.calculation_formula = resolved_formula
        target.formula_item_refs = list(validation.item_references or [])
        target.formula_unresolved_refs = list(unresolved_refs or [])

        if unresolved_refs:
            unresolved_text = ", ".join(f"[{ref}]" for ref in unresolved_refs)
            self._append_unique_note(
                proposal,
                f"Formula warning: unresolved references {unresolved_text}.",
            )
            # Include compact "did you mean" hints for the first unresolved ref.
            first_ref = unresolved_refs[0]
            hint = suggestions.get(first_ref) or []
            if hint:
                self._append_unique_note(
                    proposal,
                    f"Formula warning: did you mean one of {', '.join(hint[:3])} for [{first_ref}]?",
                )
            self._append_effect_note(
                proposal,
                f"Stored formula for '{target.name}' (unresolved refs: {unresolved_text}).",
            )
        else:
            self._append_effect_note(
                proposal,
                f"Applied formula to {target.name}: {target.calculation_formula}",
            )

    def _apply_formula_clear_request(self, proposal: GradebookProposal, prompt: str) -> None:
        """Clear existing formulas based on explicit clear/remove formula directives."""
        text_l = (prompt or "").lower()
        if not re.search(r"\b(?:clear|remove|delete|unset)\b.*\bformula\b", text_l):
            return

        target_hint = ""
        target_match = re.search(
            r"\b(?:clear|remove|delete|unset)\s+(?:the\s+)?formula\s+(?:from|for|on)\s+([a-zA-Z][a-zA-Z ]{1,40})",
            text_l,
        )
        if not target_match:
            target_match = re.search(
                r"\b(?:clear|remove|delete|unset)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,40})\s+formula\b",
                text_l,
            )
        if target_match:
            target_hint = target_match.group(1).strip().rstrip(".,")

        targets = self._resolve_category_targets(proposal, target_hint)
        for cat in targets:
            if not getattr(cat, "calculation_formula", None):
                continue
            cat.calculation_formula = None
            cat.formula_item_refs = []
            cat.formula_unresolved_refs = []
            self._append_effect_note(proposal, f"Cleared formula from {cat.name}")

    @staticmethod
    def _parse_aggregation_method(prompt: str) -> Optional[int]:
        text_l = (prompt or "").lower()
        _VALID_METHOD_IDS = {0, 10, 11, 12, 13}

        # Direct numeric method ID: "method number 13", "method 13", "number 13", "use number 13"
        numeric_m = re.search(r'\b(?:method\s+(?:number\s+)?|number\s+)(\d+)\b', text_l)
        if numeric_m:
            n = int(numeric_m.group(1))
            if n in _VALID_METHOD_IDS:
                return n

        # Only treat text as aggregation intent when method/aggregation context is explicit.
        has_agg_context = bool(
            re.search(r"\b(?:aggregation|aggregate|method|grade\s+aggregation)\b", text_l)
            or re.search(r"\b(?:use|set|switch|change)\b", text_l)
            or any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted", "mean of grades", "simple mean", "natural"))
        )

        if has_agg_context and any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted")):
            if "simple" in text_l:
                return 11  # Simple weighted mean
            return 10  # Weighted mean

        if has_agg_context and any(term in text_l for term in ("mean of grades", "simple mean", "mean")):
            if "extra credit" in text_l or "extra credits" in text_l:
                return 12  # Mean with extra credits
            return 0  # Mean of grades

        if has_agg_context and any(term in text_l for term in ("natural", "moodle default", "default aggregation")):
            return 13

        if has_agg_context and ("extra credit" in text_l or "extra credits" in text_l):
            return 12

        return None

    @staticmethod
    def _get_aggregation_method_name(method: int) -> str:
        return {
            0: "Mean of grades",
            10: "Weighted mean of grades",
            11: "Simple weighted mean of grades",
            12: "Mean of grades (with extra credits)",
            13: "Natural",
        }.get(int(method), f"Method {method}")

    def _apply_aggregation_method(self, proposal: GradebookProposal, prompt: str) -> None:
        method = self._parse_aggregation_method(prompt)
        if method is None:
            return
        old_method = proposal.aggregation_method
        proposal.aggregation_method = method
        if old_method != method:
            name = self._get_aggregation_method_name(method)
            self._append_effect_note(proposal, f"Aggregation method set to {name}")

    def _apply_category_settings(self, proposal: GradebookProposal, prompt: str) -> None:
        """Parse and apply drop_lowest, keep_highest, extra_credit, aggregate_only_graded per category."""
        prompt_lower = prompt.lower()

        # Scoped phrasing: "For Assignments, set drop lowest to 1"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*set\s+drop\s+lowest\s+(?:to\s+)?(\d+)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            n = int(m.group(2))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.drop_lowest = n
                cat.keep_highest = 0
                self._append_effect_note(proposal, f"Drop lowest {n} from {cat.name}")

        # drop lowest N [from category]
        for m in re.finditer(
            r"\bdrop\s+(?:the\s+)?(?:lowest\s+)?(\d+)\s+(?:lowest\s+)?(?:grade[s]?\s+)?(?:from\s+)?([a-zA-Z][a-zA-Z ]{1,40})?",
            prompt_lower,
        ):
            n = int(m.group(1))
            cat_hint = (m.group(2) or "").strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.drop_lowest = n
                cat.keep_highest = 0  # mutually exclusive
                self._append_effect_note(proposal, f"Drop lowest {n} from {cat.name}")

        # Scoped phrasing: "For Labs, keep highest 2 items"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*keep\s+(?:the\s+)?(?:highest|top|best)\s+(\d+)(?:\s+items?)?",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            n = int(m.group(2))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.keep_highest = n
                cat.drop_lowest = 0
                self._append_effect_note(proposal, f"Keep highest {n} from {cat.name}")

        # keep highest / best N [from category]
        for m in re.finditer(
            r"\bkeep\s+(?:the\s+)?(?:(?:top|best|highest)\s+)?(\d+)\s+(?:top\s+|best\s+|highest\s+)?(?:grade[s]?\s+)?(?:from\s+)?([a-zA-Z][a-zA-Z ]{1,40})?",
            prompt_lower,
        ):
            n = int(m.group(1))
            cat_hint = (m.group(2) or "").strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.keep_highest = n
                cat.drop_lowest = 0  # mutually exclusive
                self._append_effect_note(proposal, f"Keep highest {n} from {cat.name}")

        # extra credit: "assignments count as extra credit" / "extra credit for labs"
        for m in re.finditer(
            r"\b([a-zA-Z][a-zA-Z ]{1,30})\s+(?:(?:count|counts|is|are)\s+(?:as\s+)?)?extra\s+credit",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.extra_credit = True

        for m in re.finditer(
            r"\bextra\s+credit\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.extra_credit = True

        # include/exclude empty grades: "include empty" / "exclude empty for quizzes"
        if re.search(r"\binclude\s+empty\b", prompt_lower):
            cat_m = re.search(r"\binclude\s+empty\s+(?:grades?\s+)?(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = False
                self._append_effect_note(proposal, f"{cat.name} includes empty grades in aggregation")

        if re.search(r"\bexclude\s+empty\b", prompt_lower):
            cat_m = re.search(r"\bexclude\s+empty\s+(?:grades?\s+)?(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = True
                self._append_effect_note(proposal, f"{cat.name} aggregates only non-empty grades")

        # Alternate phrasing: "aggregate only non-empty grades" / "only graded"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*(?:set\s+)?aggregate\s+only\s+(?:non[-\s]?empty|graded)\s+grades?",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = True
                self._append_effect_note(proposal, f"{cat.name} aggregates only non-empty grades")

        # aggregate outcomes: include or exclude outcome items in aggregation
        if re.search(r"\b(?:include|aggregate)\s+outcomes?\b", prompt_lower):
            cat_m = re.search(r"\b(?:include|aggregate)\s+outcomes?\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_outcomes = True

        if re.search(r"\b(?:exclude|ignore|do\s+not\s+aggregate)\s+outcomes?\b", prompt_lower):
            cat_m = re.search(r"\b(?:exclude|ignore|do\s+not\s+aggregate)\s+outcomes?\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_outcomes = False

        # grade_max: "max grade for assignments is 150" / "assignments out of 150"
        for m in re.finditer(
            r"(?:max(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(\d+(?:\.\d+)?)"
            r"|max(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?)"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+(?:is\s+)?out\s+of\s+(\d+(?:\.\d+)?)"
            r"|set\s+(?:max|maximum)\s+(?:grade\s+)?for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            elif m.group(3) and m.group(4):
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            elif m.group(5) and m.group(6):
                cat_hint, val = m.group(5).strip(), float(m.group(6))
            else:
                cat_hint, val = m.group(7).strip(), float(m.group(8))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_max = val

        # grade_pass: "passing grade for labs is 60" / "pass labs at 60%"
        for m in re.finditer(
            r"(?:passing\s+(?:grade|mark|score)\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(\d+(?:\.\d+)?)"
            r"|passing\s+(?:grade|mark|score)\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?)"
            r"|pass\s+([a-zA-Z][a-zA-Z ]{1,30})\s+(?:at|with)\s+(\d+(?:\.\d+)?)"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+pass(?:ing)?\s+(?:is\s+)?(?:at\s+)?(\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            elif m.group(3) and m.group(4):
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            elif m.group(5) and m.group(6):
                cat_hint, val = m.group(5).strip(), float(m.group(6))
            else:
                cat_hint, val = m.group(7).strip(), float(m.group(8))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_pass = val

        # grade_min: "minimum grade for labs is 0" / "set min for assignments to 10"
        for m in re.finditer(
            r"(?:min(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(-?\d+(?:\.\d+)?)"
            r"|set\s+min(?:imum)?\s+(?:grade\s+)?for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(-?\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            else:
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_min = val

        # hide until: "hide assignments until 2026-12-20"
        for m in re.finditer(
            r"\bhide\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:until|untill)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {m.group(2).strip()}")

        # "set/make X as hidden until 2026-05-19"
        for m in re.finditer(
            r"\b(?:set|make)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:as\s+)?hidden\s+(?:until|untill)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {m.group(2).strip()}")

        # Relative-date phrasing for both "set/make X as hidden until ..." and "hide X until ...".
        # Supports: "next week", "in 2 months", "3 weeks from now", "tomorrow", "in 5 days"
        for m in re.finditer(
            r"\b(?:set|make|hide)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:as\s+)?(?:hidden\s+)?(?:until|untill)\s+"
            r"(next\s+week|tomorrow|next\s+month|(?:in\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:weeks?|months?|days?)(?:\s+from\s+now)?)(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            date_part = m.group(2).strip()
            ts = self._parse_relative_date_to_timestamp(date_part)
            display_text = date_part
            
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {display_text}")

        # hidden: "hide assignments from students" / "hide midterm"
        # Keep this after hide-until handlers so date expressions are not swallowed.
        for m in re.finditer(
            r"\bhide\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?=\s+(?:category|from\s+students?)\b|\s*$)(?:\s+(?:category|from\s+students?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip()
            if re.search(r"\b(?:until|untill)\b", cat_hint):
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                self._append_effect_note(proposal, f"{cat.name} hidden from students")

        for m in re.finditer(
            r"\b(?:show|unhide|reveal)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+category)?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = False
                cat.hidden_until = None
                self._append_effect_note(proposal, f"{cat.name} is visible to students")

        # locked: "lock the final exam" / "unlock assignments"
        for m in re.finditer(
            r"\block\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:category|grades?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = True
                cat.lock_time = None

        # schedule lock: "lock labs until 2026-10-01"
        for m in re.finditer(
            r"\block\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:until|on|at)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = False
                cat.lock_time = ts

        for m in re.finditer(
            r"\bunlock\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:category|grades?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = False
                cat.lock_time = None

        # display_type: "show assignments as percentage" / "display labs as letter"
        display_map = {
            r"\bpercentage\b": GRADE_DISPLAY_TYPE_PERCENTAGE,
            r"\bletter\b": GRADE_DISPLAY_TYPE_LETTER,
            r"\breal\b|\bnumeric\b|\bnumber\b": GRADE_DISPLAY_TYPE_REAL,
            r"\breal\s+and\s+percentage\b|\bpercentage\s+and\s+real\b": GRADE_DISPLAY_TYPE_REAL_PERCENTAGE,
            r"\breal\s+and\s+letter\b|\bletter\s+and\s+real\b": GRADE_DISPLAY_TYPE_REAL_LETTER,
            r"\bdefault\s+display\b": GRADE_DISPLAY_TYPE_DEFAULT,
        }
        for m in re.finditer(
            r"(?:show|display|format)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:grades?\s+)?as\s+([a-zA-Z][a-zA-Z +]*)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            disp_text = m.group(2).strip()
            resolved_type = None
            for pattern, dtype in display_map.items():
                if re.search(pattern, disp_text):
                    resolved_type = dtype
                    break
            if resolved_type is not None:
                targets = self._resolve_category_targets(proposal, cat_hint)
                for cat in targets:
                    cat.display_type = resolved_type

        # decimals: "use 2 decimal places for assignments" / "show 0 decimals for labs"
        for m in re.finditer(
            r"(?:use|show|set)\s+(\d)\s+decimal(?:\s+place)?s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+(?:use|show|with)\s+(\d)\s+decimal(?:\s+place)?s?",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                val, cat_hint = int(m.group(1)), m.group(2).strip()
            else:
                val, cat_hint = int(m.group(4)), m.group(3).strip()
            val = max(0, min(5, val))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.decimals = val

    @staticmethod
    def _parse_iso_date_to_timestamp(date_text: str) -> Optional[int]:
        """Parse YYYY-MM-DD into a UTC timestamp at 00:00:00."""
        try:
            dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return int(dt.timestamp())

    @staticmethod
    def _parse_relative_date_to_timestamp(relative_text: str) -> Optional[int]:
        text = (relative_text or "").strip().lower()
        now = datetime.now(timezone.utc)
        _WORD_NUMS = {
            'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        }

        if text == "tomorrow":
            target = now + timedelta(days=1)
        elif text == "next week":
            target = now + timedelta(days=7)
        elif text == "next month":
            target = now + timedelta(days=30)
        else:
            # "two weeks from now" / "in 2 weeks" / "3 weeks from now"
            m_weeks = re.match(
                r'^(?:in\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+weeks?(?:\s+from\s+now)?$',
                text,
            )
            if m_weeks:
                raw = m_weeks.group(1)
                n = int(raw) if raw.isdigit() else _WORD_NUMS.get(raw, 1)
                target = now + timedelta(weeks=n)
            else:
                # "in N months" / "N months from now" / "next month" already handled
                m_months = re.match(
                    r'^(?:in\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+months?(?:\s+from\s+now)?$',
                    text,
                )
                if m_months:
                    raw = m_months.group(1)
                    n = int(raw) if raw.isdigit() else _WORD_NUMS.get(raw, 1)
                    # Add months by year+month arithmetic to handle month lengths correctly
                    month = now.month + n
                    year = now.year
                    while month > 12:
                        month -= 12
                        year += 1
                    try:
                        target = now.replace(year=year, month=month, day=1)
                    except ValueError:
                        # Handle day overflow (e.g., Jan 31 + 1 month -> Feb 28/29)
                        target = now.replace(year=year, month=month + 1, day=1) - timedelta(days=1)
                else:
                    # "in N days"
                    m_days = re.match(r'^in\s+(\d+)\s+days?$', text)
                    if m_days:
                        target = now + timedelta(days=int(m_days.group(1)))
                    else:
                        return None

        target = target.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(target.timestamp())

    def _resolve_category_targets(self, proposal: GradebookProposal, hint: str) -> List:
        """Return matching categories for a hint string, or all categories if hint is empty."""
        if not hint:
            return list(proposal.categories)
        resolved = self._resolve_category_name(proposal, hint)
        if resolved:
            for cat in proposal.categories:
                if cat.name == resolved:
                    return [cat]
        return []

    def _parse_remaining_target(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        match = re.search(
            r"\b(?:give|assign|put|allocate)\s+(?:the\s+)?(?:remaining|remai\w*|left(?:over)?)\s+(?:weight\s+)?(?:to|ot|into)\s+([a-zA-Z][a-zA-Z ]{1,40})",
            prompt,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return self._resolve_category_name(proposal, match.group(1).strip())

    def _apply_remaining_weight_directive(
        self,
        proposal: GradebookProposal,
        weights: Dict[str, float],
        remaining_target: str,
    ) -> Dict[str, float]:
        current = {cat.name: float(cat.weight) for cat in proposal.categories}
        for name, value in weights.items():
            current[name] = float(value)

        remainder = 100.0 - sum(
            weight for name, weight in current.items()
            if name.lower() != remaining_target.lower()
        )
        weights[remaining_target] = round(remainder, 2)
        return weights

    def _rebalance_weights(
        self,
        proposal: GradebookProposal,
        weights: Dict[str, float],
        remaining_target: str,
    ) -> Dict[str, float]:
        current = {cat.name: float(cat.weight) for cat in proposal.categories}
        for name, value in weights.items():
            current[name] = float(value)

        remainder = 100.0 - sum(
            weight for name, weight in current.items()
            if name.lower() != remaining_target.lower()
        )
        weights[remaining_target] = round(remainder, 2)
        return weights

    def _apply_directive_normalization(
        self,
        proposal: GradebookProposal,
        prompt: str,
        explicit_updates: Dict[str, float],
    ) -> None:
        if not explicit_updates:
            return

        prompt_lower = prompt.lower()

        # "decrease final accordingly" / "increase labs ... decrease final accordingly"
        if any(token in prompt_lower for token in ("accordingly", "decrease", "increase")):
            target = self._parse_accordingly_target(proposal, prompt)
            if target:
                self._adjust_single_category_to_target_total(proposal, target)
                return

        # "redistribute proportionally across other categories"
        if "proportion" in prompt_lower and any(word in prompt_lower for word in ("redistribute", "distribute")):
            self._auto_fix_total_to_100(proposal, explicit_updates=explicit_updates)
            return

        # Generic normalization requests.
        if self._NORMALIZE_PATTERNS.search(prompt):
            self._auto_fix_total_to_100(proposal, explicit_updates=explicit_updates)

    def _parse_accordingly_target(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        match = re.search(
            r"\b(?:decrease|increase|adjust|reduce)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+accordingly",
            prompt,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return self._resolve_category_name(proposal, match.group(1).strip())

    def _adjust_single_category_to_target_total(self, proposal: GradebookProposal, category_name: str) -> None:
        target = next((cat for cat in proposal.categories if cat.name.lower() == category_name.lower()), None)
        if target is None:
            return

        other_total = sum(cat.weight for cat in proposal.categories if cat is not target)
        target.weight = round(100.0 - other_total, 2)
        proposal.notes.append(f"Adjusted '{target.name}' to keep total exactly 100%.")

    def _auto_fix_total_to_100(self, proposal: GradebookProposal, explicit_updates: Dict[str, float]) -> None:
        """
        Ensure total category weight remains exactly 100 after explicit updates.

        Strategy:
          - If there are untouched categories, scale ONLY the untouched ones to fill the remaining weight.
            (Keeps the instructor-specified categories exact.)
          - If all categories were explicitly set, adjust the last-updated category by the delta.
        """
        if not proposal.categories:
            return

        # Normalize key set to existing category names (case-insensitive).
        explicit_keys = {name.lower() for name in explicit_updates.keys()}
        cats = list(proposal.categories)
        untouched = [c for c in cats if c.name.lower() not in explicit_keys]
        touched = [c for c in cats if c.name.lower() in explicit_keys]

        total = sum(c.weight for c in cats)
        if abs(total - 100.0) <= 0.1:
            return

        target_remaining = 100.0 - sum(c.weight for c in touched)

        if untouched:
            current_untouched_total = sum(c.weight for c in untouched)
            if abs(current_untouched_total) < 0.001:
                # No usable baseline to scale; distribute evenly.
                even = target_remaining / len(untouched)
                for c in untouched:
                    c.weight = round(even, 2)
            else:
                scale = target_remaining / current_untouched_total
                for c in untouched:
                    c.weight = round(c.weight * scale, 2)

            # Final micro-adjust to eliminate rounding drift (apply to the last untouched).
            new_total = sum(c.weight for c in cats)
            delta = 100.0 - new_total
            if abs(delta) > 0.001:
                untouched[-1].weight = round(untouched[-1].weight + delta, 2)

            proposal.notes.append("Auto-normalized remaining category weights to keep total exactly 100%.")
            return

        # If everything was touched, adjust the last explicit category.
        # Dict preserves insertion order (py3.7+), and our parser overwrites duplicates,
        # so this is a reasonable proxy for "last mentioned".
        last_name = next(reversed(explicit_updates.keys()), None)
        if last_name:
            last = next((c for c in cats if c.name.lower() == last_name.lower()), None)
            if last is not None:
                delta = 100.0 - total
                last.weight = round(last.weight + delta, 2)
                proposal.notes.append(f"Auto-normalized '{last.name}' by {delta:+.2f}% to keep total 100%.")

    # Collect names of split subcategories so they are excluded from weight-parsing
    # (prevents "Homework 15%" being misread as "Assignments 15%" via alias).
    @staticmethod
    def _extract_split_piece_names(prompt: str) -> List[str]:
        match = re.search(r"\b(?:split|divide)\b.+?\binto\b(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"\badd\s+subcategories\s*[:;]?\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            return []
        pieces = re.split(r",| and | & ", match.group(1))
        names = []
        for piece in pieces:
            part = re.sub(r"\d+(?:\.\d+)?\s*%.*", "", piece).strip()
            part = re.sub(r"\b(but|keep|as|the|parent|category)\b", " ", part, flags=re.IGNORECASE).strip()
            if part:
                names.append(part.lower())
        return names

    def _parse_weight_assignments(
        self,
        proposal: GradebookProposal,
        prompt: str,
        skip_split_pieces: bool = False,
    ) -> Dict[str, float]:
        # Handle "from X% to Y%" — only use the destination value Y.
        clean_prompt = re.sub(
            r"(\bfrom\s+\d+(?:\.\d+)?\s*%\s*to\b)",
            "to",
            prompt,
            flags=re.IGNORECASE,
        )

        split_piece_names: List[str] = []
        if skip_split_pieces:
            split_piece_names = self._extract_split_piece_names(prompt)
            # Also strip split payload text to avoid parsing subcategory weights as top-level weights.
            clean_prompt = re.sub(r"\bsplit\b.+", "", clean_prompt, flags=re.IGNORECASE).strip()
            clean_prompt = re.sub(r"\bdivide\b.+", "", clean_prompt, flags=re.IGNORECASE).strip()
            clean_prompt = re.sub(r"\badd\s+subcategories\b\s*[:;]?.+", "", clean_prompt, flags=re.IGNORECASE).strip()

        # Guard against parsing non-weight numeric settings (grade min/max/pass, dates, decimals, hide/lock).
        settings_context = bool(re.search(
            r"\b("
            r"minimum|maximum|max|min|passing|grade\s+pass|grade\s+max|grade\s+min"
            r"|decimal|display|percentage|letter|real"
            r"|hide|hidden|show|unhide|reveal|lock|unlock|until|on\s+\d{4}-\d{2}-\d{2}"
            r"|drop\s+lowest|keep\s+(?:top|best|highest)"
            r"|include\s+empty|exclude\s+empty|outcomes?|extra\s+credit"
            r")\b",
            clean_prompt,
            flags=re.IGNORECASE,
        ))

        matches = []

        # Explicit percentage always counts as weight intent.
        percent_pattern = re.compile(
            r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\s*%",
            flags=re.IGNORECASE,
        )
        matches.extend(percent_pattern.findall(clean_prompt))
        matches.extend(re.findall(
            r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s+(\d+(?:\.\d+)?)\s*%(?=\D|$)",
            clean_prompt,
            flags=re.IGNORECASE,
        ))

        # Explicit "weight" keyword counts even without %.
        weight_keyword_pattern = re.compile(
            r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)\s+weight\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\b",
            flags=re.IGNORECASE,
        )
        matches.extend(weight_keyword_pattern.findall(clean_prompt))

        # Generic no-% parsing is only safe when prompt is not about other numeric settings.
        if not settings_context:
            # Imperative no-% phrasing: "make labs 20" / "set assignments 35".
            # This restores compact commands without requiring "to/is" or "%".
            direct_no_percent_pattern = re.compile(
                r"\b(?:set|make|change|adjust|update|increase|decrease)\s+"
                r"([a-zA-Z][a-zA-Z ]{1,40}?)\s+(\d+(?:\.\d+)?)\b",
                flags=re.IGNORECASE,
            )
            matches.extend(direct_no_percent_pattern.findall(clean_prompt))

            # Chained no-% assignments: "make midterm 24 and final 36".
            # Parse each category-number pair so both updates are applied.
            chained_no_percent_pattern = re.compile(
                r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s+(\d+(?:\.\d+)?)(?=\s*(?:,|and\b|$))",
                flags=re.IGNORECASE,
            )
            matches.extend(chained_no_percent_pattern.findall(clean_prompt))

            generic_pattern = re.compile(
                r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
                r"([a-zA-Z][a-zA-Z ]{1,40}?)\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\b",
                flags=re.IGNORECASE,
            )
            matches.extend(generic_pattern.findall(clean_prompt))

            # Support compact category lists like "Assignments 35, Labs 15".
            if re.search(r"\b(?:gradebook\s+with|categories|weights?)\b", clean_prompt, flags=re.IGNORECASE):
                matches.extend(re.findall(
                    r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s*(\d+(?:\.\d+)?)\s*%?\b",
                    clean_prompt,
                    flags=re.IGNORECASE,
                ))

        weights: Dict[str, float] = {}
        allow_new_categories = self._allow_new_categories_from_prompt(prompt)
        for raw_name, raw_weight in matches:
            if skip_split_pieces and raw_name.strip().lower() in split_piece_names:
                continue
            resolved = self._resolve_category_name(proposal, raw_name)
            if resolved is None and allow_new_categories:
                candidate = self._clean_new_category_name(raw_name)
                if candidate:
                    resolved = candidate
            if resolved is None:
                continue
            # Last occurrence wins for the same category (handles repeat mentions).
            weights[resolved] = float(raw_weight)
        return weights

    @staticmethod
    def _allow_new_categories_from_prompt(prompt: str) -> bool:
        prompt_l = prompt.lower()
        if re.search(r"\b(do\s+not\s+create|don't\s+create|dont\s+create|dont\s+add|don't\s+add|keep\s+categories\s+unchanged|only\s+rebalance|keep\s+all\s+else)\b", prompt_l):
            return False
        return bool(re.search(r"\b(create|build|propose|start\s+with|gradebook\s+with|categories?)\b", prompt_l))

    @staticmethod
    def _clean_new_category_name(raw_name: str) -> Optional[str]:
        name = re.sub(r"\s+", " ", (raw_name or "").strip())
        name = re.sub(r"^[^a-zA-Z]+|[^a-zA-Z]+$", "", name)
        if not name:
            return None
        if len(name.split()) > 3:
            return None
        return name.title()

    @staticmethod
    def _should_rebuild_category_set(prompt: str, weights: Dict[str, float]) -> bool:
        if len(weights) < 3:
            return False
        prompt_l = prompt.lower()
        if re.search(r"\b(change|adjust|update|increase|decrease|rebalance|remove|split|undo|redo)\b", prompt_l):
            return False
        return bool(re.search(r"\b(create|build|propose|start\s+with)\b", prompt_l))

    @staticmethod
    def _rebuild_categories_from_weights(proposal: GradebookProposal, weights: Dict[str, float]) -> List[GradebookCategory]:
        existing = {cat.name.lower(): cat for cat in proposal.categories}
        rebuilt: List[GradebookCategory] = []
        for name, weight in weights.items():
            src = existing.get(name.lower())
            rebuilt.append(
                GradebookCategory(
                    name=name,
                    weight=float(weight),
                    items=list(src.items) if src else [],
                    drop_lowest=getattr(src, "drop_lowest", 0) if src else 0,
                    keep_highest=getattr(src, "keep_highest", 0) if src else 0,
                    aggregate_only_graded=getattr(src, "aggregate_only_graded", True) if src else True,
                    aggregate_outcomes=getattr(src, "aggregate_outcomes", False) if src else False,
                    extra_credit=getattr(src, "extra_credit", False) if src else False,
                    grade_min=getattr(src, "grade_min", None) if src else None,
                    grade_max=getattr(src, "grade_max", 100.0) if src else 100.0,
                    grade_pass=getattr(src, "grade_pass", None) if src else None,
                    hidden=getattr(src, "hidden", False) if src else False,
                    hidden_until=getattr(src, "hidden_until", None) if src else None,
                    locked=getattr(src, "locked", False) if src else False,
                    lock_time=getattr(src, "lock_time", None) if src else None,
                    display_type=getattr(src, "display_type", GRADE_DISPLAY_TYPE_DEFAULT) if src else GRADE_DISPLAY_TYPE_DEFAULT,
                    decimals=getattr(src, "decimals", -1) if src else -1,
                    calculation_formula=getattr(src, "calculation_formula", None) if src else None,
                    formula_item_refs=list(getattr(src, "formula_item_refs", []) or []) if src else [],
                    formula_unresolved_refs=list(getattr(src, "formula_unresolved_refs", []) or []) if src else [],
                )
            )
        return rebuilt

    def _apply_weight_updates(self, proposal: GradebookProposal, weights: Dict[str, float]) -> None:
        normalized_map = {cat.name.lower(): cat for cat in proposal.categories}
        for name, weight in weights.items():
            key = name.lower()
            if key in normalized_map:
                existing = normalized_map[key]
                if abs(float(existing.weight) - float(weight)) > 0.001:
                    existing.weight = weight
                    self._append_effect_note(proposal, f"Set {existing.name} to {weight:.1f}%")
            else:
                # Allow adding a new category when the user explicitly sets its weight
                # (e.g., "add quizzes 5%" / "quizzes 5%").
                proposal.categories.append(GradebookCategory(name=name, weight=weight, items=[]))
                self._append_effect_note(proposal, f"Added '{name}' with weight {weight:.1f}%")

    def _parse_add_category_requests(self, proposal: GradebookProposal, prompt: str) -> List[str]:
        requested: List[str] = []
        seen: set = set()

        for match in re.finditer(
            r"\b(?:add|added)\s+(?:new\s+)?(?:category\s+)?(?:as\s+)?([a-z][a-z\s]{1,40}?)(?=(?:\s+with\b|\s+at\b|\s+to\b|\s*[:.,;!?]|$))",
            prompt,
            flags=re.IGNORECASE,
        ):
            raw = re.sub(r"\s+", " ", (match.group(1) or "").strip())
            if not raw:
                continue
            if re.search(r"\b(?:it|them|its\s+weight|subcategor(?:y|ies))\b", raw, flags=re.IGNORECASE):
                continue

            resolved = self._resolve_category_name(proposal, raw)
            if not resolved:
                cleaned = self._clean_new_category_name(raw)
                if not cleaned:
                    continue
                resolved = cleaned

            key = resolved.lower()
            if key in seen:
                continue
            seen.add(key)
            requested.append(resolved)

        return requested

    def _extract_recent_removed_transfer(self, proposal: GradebookProposal, category_name: str) -> Optional[Tuple[float, str]]:
        category_l = (category_name or "").strip().lower()
        if not category_l:
            return None

        for note in reversed(proposal.notes or []):
            text = str(note or "")
            if text.startswith("Effect:"):
                text = text[len("Effect:"):].strip()
            match = re.match(
                r"Removed\s+'([^']+)'\s+and\s+assigned\s+([0-9]+(?:\.[0-9]+)?)%\s+to\s+(.+)$",
                text,
                flags=re.IGNORECASE,
            )
            if not match:
                continue

            removed_name = (match.group(1) or "").strip().lower()
            if removed_name != category_l:
                continue

            try:
                weight = float(match.group(2))
            except Exception:
                continue

            target_name = self._resolve_category_name(proposal, (match.group(3) or "").strip())
            if not target_name:
                continue

            target = next((c for c in proposal.categories if c.name.lower() == target_name.lower()), None)
            if target is None or float(getattr(target, "weight", 0.0)) < weight:
                continue

            return weight, target.name

        return None

    def _apply_add_category_requests(
        self,
        proposal: GradebookProposal,
        prompt: str,
        explicit_updates: Dict[str, float],
    ) -> None:
        requested = self._parse_add_category_requests(proposal, prompt)
        if not requested:
            return

        explicit_keys = {k.lower() for k in (explicit_updates or {}).keys()}
        existing_map = {cat.name.lower(): cat for cat in proposal.categories}

        for name in requested:
            key = name.lower()
            if key in explicit_keys or key in existing_map:
                continue

            inferred_weight = 0.0
            proposal.categories.append(GradebookCategory(name=name, weight=inferred_weight, items=[]))
            self._append_effect_note(proposal, f"Added '{name}' with weight {inferred_weight:.1f}%")

    def _detect_split_parent(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        # 1) "Split Assignments ... into ..."
        direct = re.search(r"\b(?:split|divide)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(|\s+into\b)", prompt, flags=re.IGNORECASE)
        if direct:
            resolved = self._resolve_category_name(proposal, direct.group(1).strip())
            if resolved:
                return resolved

        # 2) "In Labs, ... split/divide into ..." or "For Labs, ..."
        scoped = re.search(
            r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,.*\b(?:split|divide)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        if scoped:
            resolved = self._resolve_category_name(proposal, scoped.group(1).strip())
            if resolved:
                return resolved

        # 3) "In Labs, add subcategories:" or "For Labs, add subcategories:"
        add_subs = re.search(
            r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,.*\badd\s+subcategories\b",
            prompt,
            flags=re.IGNORECASE,
        )
        if add_subs:
            resolved = self._resolve_category_name(proposal, add_subs.group(1).strip())
            if resolved:
                return resolved

        return None

    def _parse_split_categories(self, prompt: str) -> List[Tuple[str, Optional[float]]]:
        # Try "split/divide X into Y 10%, Z 15%" pattern
        match = re.search(r"\b(?:split|divide)\b.+?\binto\b\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            # Try "In X, add subcategories: Y 10%, Z 15%" pattern
            match = re.search(r"\badd\s+subcategories\s*[:;]?\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            return []

        raw_list = re.sub(r"\bbut\b.+", "", match.group(1), flags=re.IGNORECASE).strip()
        each_weight = None
        each_match = re.search(r"\b(?:assign|set|make)?\s*(\d+(?:\.\d+)?)\s*%\s*(?:for\s*)?each\b", raw_list, flags=re.IGNORECASE)
        if each_match:
            each_weight = float(each_match.group(1))
            raw_list = re.sub(r"\b(?:and\s+)?(?:assign|set|make)?\s*\d+(?:\.\d+)?\s*%\s*(?:for\s*)?each\b", "", raw_list, flags=re.IGNORECASE)

        pieces = re.split(r",| and | & ", raw_list)
        parsed: List[Tuple[str, Optional[float]]] = []
        for piece in pieces:
            cleaned = piece.strip()
            if not cleaned:
                continue
            weighted = re.match(r"([a-zA-Z][a-zA-Z\- ]*?)\s*(\d+(?:\.\d+)?)\s*%", cleaned, flags=re.IGNORECASE)
            if weighted:
                name = weighted.group(1).strip()
                name = re.sub(r"^[^a-zA-Z]+", "", name)
                name = re.sub(r"[^a-zA-Z]+$", "", name)
                if name:
                    parsed.append((name.title(), float(weighted.group(2))))
            elif re.search(r"[a-zA-Z]", cleaned):
                name = re.sub(r"\d+(?:\.\d+)?\s*%", "", cleaned).strip()
                name = re.sub(r"^[^a-zA-Z]+", "", name)
                name = re.sub(r"[^a-zA-Z]+$", "", name)
                if name:
                    parsed.append((name.title(), None))

        if each_weight is not None and parsed and all(weight is None for _, weight in parsed):
            parsed = [(name, each_weight) for name, _ in parsed]

        return parsed

    def _apply_split_request(self, proposal: GradebookProposal, prompt: str) -> None:
        parent_name = self._detect_split_parent(proposal, prompt)
        split_parts = self._parse_split_categories(prompt)
        if not parent_name or not split_parts:
            return

        parent = next((cat for cat in proposal.categories if cat.name.lower() == parent_name.lower()), None)
        if parent is None:
            return

        # Always overwrite previous subcategories and remove prior split notes for this parent
        parent.subcategories = []
        proposal.notes = [n for n in (proposal.notes or []) if not (str(n).startswith(f"Effect: In {parent.name}, split into") or str(n).startswith(f"{parent.name} split requested"))]

        subcategories: List[GradebookSubcategory] = []
        labels: List[str] = []
        for name, weight in split_parts:
            w = float(weight) if weight is not None else 0.0
            subcategories.append(GradebookSubcategory(name=name, weight=w))
            labels.append(f"{name} {w:.1f}%" if weight is not None else name)

        parent.subcategories = subcategories
        self._append_effect_note(
            proposal,
            f"In {parent.name}, split into " + ", ".join(labels),
        )
        self._append_unique_note(
            proposal,
            f"{parent.name} split requested (treated as internal allocation): " + ", ".join(labels) + ".",
        )

    # Redistribution intent keywords for removal prompts.
    _REDISTRIBUTE_EVENLY_PATTERNS = re.compile(
        r"\b(?:evenly|equally|proportionally|split\s+evenly|distribute\s+evenly|"
        r"redistribute|divide\s+(?:it\s+)?equally|share\s+equally|spread\s+equally)\b",
        re.IGNORECASE,
    )
    _REDISTRIBUTE_TO_PATTERNS = re.compile(
        r"\band\s+(?:give\s+(?:its\s+)?weight\s+to|give\s+to|assign\s+(?:it\s+)?to|"
        r"move\s+(?:weight\s+)?to|reallocate\s+to|redistribute\s+(?:to|among)|"
        r"distribute\s+to|split\s+(?:weight\s+)?among|split\s+(?:it\s+)?between|"
        r"add\s+(?:it\s+)?to)\s+([a-z\s,&]+)",
        re.IGNORECASE,
    )

    def _apply_removals(self, proposal: GradebookProposal, prompt: str) -> None:
        """Handle category removal with three distinct weight modes.

        Supports:
        1. ``remove X``                     – free the weight (total decreases)
        2. ``remove X evenly``              – redistribute freed weight to remaining categories
        3. ``remove X and give/assign to Y``– transfer freed weight to specific category Y
        """
        def _clean_phrase(value: str) -> str:
            cleaned = re.sub(
                r"\b(?:the|a|an|its|lowest|highest|category|categories|weight|one|single)\b",
                " ",
                value,
                flags=re.IGNORECASE,
            )
            return re.sub(r"\s+", " ", cleaned).strip(" ,.")

        # Match any remove/delete/drop, capture everything after.
        pattern = re.compile(
            r"\b(?:remove|delete|drop)\s+(?:the\s+)?([a-z][a-z\s]{0,40}?)(?:\s+and\s|\s+evenly|\s+equally|\s+proportionally|[,:;.!?]|$)",
            re.IGNORECASE,
        )

        handled: set = set()


        for match in pattern.finditer(prompt):
            raw_category = _clean_phrase(match.group(1) or "")
            resolved = self._resolve_category_name(proposal, raw_category)
            if not resolved or resolved in handled:
                # If not found, acknowledge with a note/effect.
                if not resolved:
                    self._append_effect_note(
                        proposal,
                        f"Tried to remove '{raw_category}', but category was not found. No changes made.",
                    )
                continue

            removable = next(
                (cat for cat in proposal.categories if cat.name.lower() == resolved.lower()),
                None,
            )
            if removable is None:
                self._append_effect_note(
                    proposal,
                    f"Tried to remove '{raw_category}', but category was not found. No changes made.",
                )
                continue

            handled.add(resolved)
            removed_weight = removable.weight
            proposal.categories = [cat for cat in proposal.categories if cat is not removable]

            if not proposal.categories:
                self._append_effect_note(proposal, f"Removed '{resolved}' ({removed_weight:.1f}% freed)")
                continue

            # Determine redistribution intent from the full prompt.
            tail = prompt[match.start():]  # look at the tail to catch "and give to Y"
            to_match = self._REDISTRIBUTE_TO_PATTERNS.search(tail)

            if to_match:
                # Mode 3: assign freed weight to explicit target(s).
                raw_targets = _clean_phrase(to_match.group(1) or "")
                target_str = re.sub(r"\b(?:and|or)\b", ",", raw_targets, flags=re.IGNORECASE)
                candidate_names = [t.strip().rstrip(",. ") for t in target_str.split(",") if t.strip()]
                resolved_targets = [
                    self._resolve_category_name(proposal, t)
                    for t in candidate_names
                ]
                resolved_targets = [t for t in resolved_targets if t]
                target_cats = [cat for cat in proposal.categories if cat.name in resolved_targets]

                if target_cats:
                    share = removed_weight / len(target_cats)
                    for cat in target_cats:
                        cat.weight += share
                    target_label = ", ".join(cat.name for cat in target_cats)
                    self._append_effect_note(
                        proposal,
                        f"Removed '{resolved}' and assigned {removed_weight:.1f}% to {target_label}",
                    )
                else:
                    # Target not found — fall back to free (don't silently redistribute).
                    self._append_effect_note(
                        proposal,
                        f"Removed '{resolved}' ({removed_weight:.1f}% freed; target not found)",
                    )

            elif self._REDISTRIBUTE_EVENLY_PATTERNS.search(tail):
                # Mode 2: distribute freed weight evenly across all remaining categories.
                share = removed_weight / len(proposal.categories)
                for cat in proposal.categories:
                    cat.weight += share
                self._append_effect_note(
                    proposal,
                    f"Removed '{resolved}' and distributed {removed_weight:.1f}% evenly",
                )

            else:
                # Mode 1 (default): free the weight — total decreases.
                self._append_effect_note(
                    proposal,
                    f"Removed '{resolved}' ({removed_weight:.1f}% freed)",
                )

    def _resolve_category_name(self, proposal: GradebookProposal, raw_name: str) -> Optional[str]:
        cleaned = self._normalize_name(raw_name)
        if not cleaned:
            return None

        regex_alias = self._resolve_alias_by_regex(raw_name)
        if regex_alias:
            existing = self._find_existing_name(proposal, regex_alias)
            return existing or regex_alias

        if cleaned in self._category_aliases:
            alias = self._category_aliases[cleaned]
            existing = self._find_existing_name(proposal, alias)
            return existing or alias

        existing_names = [cat.name for cat in proposal.categories]
        existing_norm_map = {self._normalize_name(name): name for name in existing_names}

        if cleaned in existing_norm_map:
            return existing_norm_map[cleaned]

        close = difflib.get_close_matches(
            cleaned,
            list(existing_norm_map.keys()) + list(self._category_aliases.keys()),
            n=1,
            cutoff=0.72,
        )
        if close:
            best = close[0]
            if best in existing_norm_map:
                return existing_norm_map[best]
            alias = self._category_aliases.get(best)
            if alias:
                existing = self._find_existing_name(proposal, alias)
                return existing or alias

        # Reject long free-text fragments to avoid creating accidental categories.
        if len(cleaned.split()) > 3:
            return None
        return None

    def _resolve_alias_by_regex(self, raw_name: str) -> Optional[str]:
        for pattern, canonical in self._category_alias_patterns:
            if pattern.search(raw_name):
                return canonical
        return None

    def _load_aliases_from_env(self) -> None:
        aliases_raw = os.environ.get("GRADEBOOK_CATEGORY_ALIASES_JSON", "").strip()
        regex_raw = os.environ.get("GRADEBOOK_CATEGORY_ALIAS_REGEX_JSON", "").strip()

        if aliases_raw:
            try:
                parsed_aliases = json.loads(aliases_raw)
                if isinstance(parsed_aliases, dict):
                    for alias, canonical in parsed_aliases.items():
                        if not isinstance(alias, str) or not isinstance(canonical, str):
                            continue
                        normalized = self._normalize_name(alias)
                        if normalized:
                            self._category_aliases[normalized] = canonical.strip()
            except Exception:
                # Keep defaults if env payload is invalid.
                pass

        if regex_raw:
            try:
                parsed_regex = json.loads(regex_raw)
                if isinstance(parsed_regex, dict):
                    compiled_patterns: List[Tuple[re.Pattern, str]] = []
                    for canonical, pattern in parsed_regex.items():
                        if not isinstance(canonical, str) or not isinstance(pattern, str):
                            continue
                        compiled_patterns.append((re.compile(pattern, flags=re.IGNORECASE), canonical.strip()))
                    if compiled_patterns:
                        # Env patterns are prepended so teams can override matching behavior.
                        self._category_alias_patterns = compiled_patterns + self._category_alias_patterns
            except Exception:
                # Keep defaults if env payload is invalid.
                pass

    def _find_existing_name(self, proposal: GradebookProposal, target_name: str) -> Optional[str]:
        target = self._normalize_name(target_name)
        for cat in proposal.categories:
            if self._normalize_name(cat.name) == target:
                return cat.name
        return None

    # Phrases that ask the agent to auto-normalize weights to sum to 100%.
    _NORMALIZE_PATTERNS = re.compile(
        r"\b("
        r"rebalance|normalize|normalise"
        r"|make\s+(?:the\s+)?total\s+(?:weight\s+)?(?:equal\s+)?100"
        r"|set\s+(?:the\s+)?total\s+(?:to\s+)?100"
        r"|round\s+(?:up\s+)?to\s+100"
        r"|total\s+(?:should\s+(?:be|equal)|must\s+be|exactly)\s+100"
        r"|keep\s+total\s+(?:exactly\s+)?100"
        r"|(?:total\s+)?(?:weight\s+)?100\s*%"
        r")\b",
        flags=re.IGNORECASE,
    )

    def _post_update_checks(self, proposal: GradebookProposal, prompt: str) -> None:
        total = sum(cat.weight for cat in proposal.categories)

        normalize_requested = bool(self._NORMALIZE_PATTERNS.search(prompt))
        if normalize_requested and proposal.categories and abs(total) > 0.001:
            scale = 100.0 / total
            for cat in proposal.categories:
                cat.weight = round(cat.weight * scale, 2)
            total = sum(cat.weight for cat in proposal.categories)
            self._append_unique_note(proposal, "Auto-normalized category weights to 100%.")

        for issue in validate_proposal_weights(proposal):
            if issue.get("path") == ["proposal"]:
                self._append_unique_note(
                    proposal,
                    f"Weight check: total is {float(issue.get('total_weight', total)):.1f}% (expected 100%)."
                )
                break

        self._check_split_consistency(proposal)
        self._check_rule_effectiveness(proposal)

    def _check_split_consistency(self, proposal: GradebookProposal) -> None:
        for parent in proposal.categories:
            parts = list(getattr(parent, "subcategories", []) or [])
            if not parts:
                continue
            split_total = sum(float(part.weight or 0) for part in parts)
            if abs(split_total - parent.weight) > 0.1:
                self._append_unique_note(
                    proposal,
                    f"{parent.name} internal split totals {split_total:.1f}% while parent weight is {parent.weight:.1f}%. "
                    "Please update split weights if you want them to match."
                )

    def _check_rule_effectiveness(self, proposal: GradebookProposal) -> None:
        for category in proposal.categories:
            child_count = 0
            if getattr(category, "subcategories", None):
                child_count = len(category.subcategories)
            elif getattr(category, "items", None):
                child_count = len(category.items)

            if child_count <= 0:
                continue

            keep_highest = int(getattr(category, "keep_highest", 0) or 0)
            if keep_highest > 0 and keep_highest >= child_count:
                self._append_unique_note(
                    proposal,
                    f"{category.name} keep-highest setting has no practical effect right now because the category has only {child_count} graded child {'item' if child_count == 1 else 'items'}."
                )

            drop_lowest = int(getattr(category, "drop_lowest", 0) or 0)
            if drop_lowest > 0 and drop_lowest >= child_count:
                self._append_unique_note(
                    proposal,
                    f"{category.name} drop-lowest setting has no practical effect right now because the category has only {child_count} graded child {'item' if child_count == 1 else 'items'}."
                )

    @staticmethod
    def _normalize_name(value: str) -> str:
        cleaned = value.lower()
        cleaned = re.sub(r"\b(set|make|change|adjust|update|keep|with|to|is|are|exactly|total|category|categories)\b", " ", cleaned)
        cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
