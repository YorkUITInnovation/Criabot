from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from .formula_parser import FormulaParser
from .schemas import CourseActivity


class FormulaResolver:
    """Resolve formula item references (e.g., hw1) to Moodle item identifiers."""

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

    @staticmethod
    def _identifier_for_activity(activity: CourseActivity) -> Optional[str]:
        # Prefer grade_item_id when available; fallback to cmid for environments
        # where dedicated grade item ids are not provided in activity payloads.
        if activity.grade_item_id is not None:
            return str(activity.grade_item_id)
        if activity.cmid is not None:
            return str(activity.cmid)
        return None

    @classmethod
    def _aliases_for_activity(cls, activity: CourseActivity) -> List[str]:
        """Generate comprehensive aliases for activity name matching.
        
        Handles various naming patterns:
        - "Homework 1" -> hw1, homework1, homework, hw
        - "Assignment" -> assignment, assign, a
        - "Midterm Exam" -> midterm, midtermexam, midterm exam, mid
        - "Final Exam" -> final, finalexam, final exam, fe
        - "Lab 1" -> lab1, lab
        - "Project" -> project, proj, p
        - "Quiz" -> quiz, q
        """
        aliases: List[str] = []
        raw_name = activity.name or ""
        module = (activity.module or "").lower().strip()

        # Always include normalized full name
        normalized_name = cls._normalize(raw_name)
        if normalized_name:
            aliases.append(normalized_name)

        # Add simple token aliases from activity name (e.g., "Homework 1" -> "homework", "1").
        tokens = re.findall(r"[A-Za-z0-9_]+", raw_name.lower())
        for token in tokens:
            n = cls._normalize(token)
            if n and len(n) >= 2:  # Only 2+ char tokens to avoid spurious matches
                aliases.append(n)

        # Extract numeric suffix if present
        number_match = re.search(r"\b(\d{1,3})\b", raw_name)
        number = number_match.group(1) if number_match else ""

        # Remove trailing numbers to get base name (e.g., "Homework 1" -> "Homework")
        base_name = re.sub(r"\s*\d+\s*$", "", raw_name).lower()
        base_normalized = cls._normalize(base_name)
        if base_normalized and base_normalized not in aliases:
            aliases.append(base_normalized)

        # Module-specific aliases based on module type and name patterns
        name_l = raw_name.lower()
        
        if module == "assign" or "assignment" in name_l or "homework" in name_l:
            if number:
                aliases.extend([f"hw{number}", f"assignment{number}", f"a{number}"])
            aliases.extend(["homework", "assignment", "assign", "hw", "a"])
            if "project" in name_l:
                aliases.extend(["project", "proj", "p"])
        
        if module == "quiz" or "quiz" in name_l or "test" in name_l:
            if number:
                aliases.extend([f"q{number}", f"quiz{number}", f"test{number}"])
            aliases.extend(["quiz", "q", "test"])
        
        if module == "lab" or "lab" in name_l:
            if number:
                aliases.extend([f"lab{number}"])
            aliases.extend(["lab"])

        # Context-aware keyword matching for special categories
        if "midterm" in name_l or "mid term" in name_l or "mid-term" in name_l:
            aliases.extend(["midterm", "midtermexam", "mid"])
        
        if "final" in name_l and "project" not in name_l:
            aliases.extend(["final", "finalexam", "fe"])
        
        if "project" in name_l and module != "assign":
            aliases.extend(["project", "proj", "p"])
        
        if "exam" in name_l and "midterm" not in name_l and "final" not in name_l:
            aliases.extend(["exam"])

        # Deduplicate while preserving order
        seen = set()
        deduped: List[str] = []
        for alias in aliases:
            key = cls._normalize(alias)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    @classmethod
    def build_activity_reference_map(cls, activities: List[CourseActivity]) -> Dict[str, str]:
        """Build alias -> item identifier map from available course activities."""
        mapping: Dict[str, str] = {}
        for activity in activities or []:
            identifier = cls._identifier_for_activity(activity)
            if not identifier:
                continue
            for alias in cls._aliases_for_activity(activity):
                mapping.setdefault(alias, identifier)
        return mapping

    @classmethod
    def build_category_reference_map(cls, category_names: List[str]) -> Dict[str, str]:
        """Build alias -> canonical category token map.

        This keeps formulas user-friendly by accepting category-like references
        (e.g., [[midterm]], [[final]]) even when they do not map to a concrete
        grade item yet. Returned values are canonical tokens, not numeric IDs.
        """
        mapping: Dict[str, str] = {}
        for raw_name in category_names or []:
            clean_name = (raw_name or "").strip()
            if not clean_name:
                continue

            canonical = cls._normalize(clean_name)
            if not canonical:
                continue

            aliases = {canonical}
            lower = clean_name.lower()

            # Token aliases from category name words.
            for token in re.findall(r"[A-Za-z0-9_]+", lower):
                norm = cls._normalize(token)
                if norm:
                    aliases.add(norm)

            # Common category shorthands.
            if "midterm" in lower or "mid term" in lower or "mid-term" in lower:
                aliases.update({"midterm", "mid", "midtermexam"})
            if "final" in lower:
                aliases.update({"final", "fe"})
            if "exam" in lower:
                aliases.add("exam")
            if "assignment" in lower or "homework" in lower:
                aliases.update({"assignments", "assignment", "homework", "hw"})
            if "lab" in lower:
                aliases.update({"labs", "lab"})
            if "quiz" in lower:
                aliases.update({"quizzes", "quiz", "q"})

            for alias in aliases:
                mapping.setdefault(alias, canonical)

        return mapping

    @classmethod
    def suggest_closest_refs(cls, unresolved_ref: str, available_aliases: List[str]) -> List[str]:
        norm_ref = cls._normalize(unresolved_ref)
        if not norm_ref or not available_aliases:
            return []
        
        # Try exact substring match first (most common pattern)
        substring_matches = [a for a in available_aliases if norm_ref in a or a in norm_ref]
        if substring_matches:
            return list(dict.fromkeys(substring_matches))[:3]  # Return top 3, deduped
        
        # Fall back to fuzzy matching with lower cutoff for partial matches
        fuzzy = difflib.get_close_matches(norm_ref, available_aliases, n=3, cutoff=0.5)
        return fuzzy if fuzzy else []

    @classmethod
    def _resolve_alias_with_fuzzy_pass(cls, ref_norm: str, ref_map: Dict[str, str]) -> Optional[str]:
        """Resolve shorthand refs with conservative fuzzy/substring matching.

        The resolver only auto-maps when the candidate identifier is unambiguous.
        """
        if not ref_norm or not ref_map:
            return None

        aliases = list(ref_map.keys())

        # 1) Number-aware shorthand matching (e.g., hw1 -> assignment1 when unique).
        num_match = re.match(r"^([a-z]+)(\d{1,3})$", ref_norm)
        if num_match:
            _, num = num_match.groups()
            number_candidates = [a for a in aliases if a.endswith(num)]
            number_ids = {ref_map[a] for a in number_candidates}
            if len(number_ids) == 1:
                return next(iter(number_ids))

        # 2) Substring-based candidates (e.g., project -> finalprojectsubmission).
        substring_candidates = [
            a for a in aliases
            if ref_norm in a or (a in ref_norm and len(a) >= 4)
        ]
        substring_ids = {ref_map[a] for a in substring_candidates}
        if len(substring_ids) == 1:
            return next(iter(substring_ids))

        # 3) High-confidence fuzzy fallback only when it points to one identifier.
        fuzzy_aliases = difflib.get_close_matches(ref_norm, aliases, n=3, cutoff=0.72)
        fuzzy_ids = {ref_map[a] for a in fuzzy_aliases}
        if len(fuzzy_ids) == 1:
            return next(iter(fuzzy_ids))

        return None

    @classmethod
    def resolve_formula(
        cls,
        formula: str,
        activities: List[CourseActivity],
        category_names: Optional[List[str]] = None,
    ) -> Tuple[str, List[str], Dict[str, List[str]]]:
        """
        Resolve formula refs to Moodle identifiers.

        Returns:
            (resolved_formula, unresolved_refs, suggestions)
        """
        ref_map = cls.build_activity_reference_map(activities)
        category_ref_map = cls.build_category_reference_map(category_names or [])
        activity_aliases = list(ref_map.keys())

        parse_result = FormulaParser.parse_formula(formula)
        refs = list(parse_result.item_references or []) if parse_result.is_valid else []
        if not refs:
            refs = [m[0] or m[1] for m in FormulaParser.ITEM_PATTERN.findall(formula)]

        item_id_map: Dict[str, str] = {}
        unresolved: List[str] = []
        suggestions: Dict[str, List[str]] = {}

        for ref in refs:
            ref_norm = cls._normalize(ref)
            resolved = ref_map.get(ref_norm)

            if resolved is None:
                resolved = cls._resolve_alias_with_fuzzy_pass(ref_norm, ref_map)
            
            if resolved is None:
                # Accept category-like refs as canonical tokens when activity ids
                # are not directly available for the reference.
                category_resolved = category_ref_map.get(ref_norm)
                if category_resolved is not None:
                    # Keep user-friendly token in formula while marking it resolved.
                    resolved = ref
                else:
                    unresolved.append(ref)
                    # Provide suggestions even for unresolved refs
                    suggestions[ref] = cls.suggest_closest_refs(ref, activity_aliases)
                    continue
            
            item_id_map[ref] = resolved

        resolved_formula = FormulaParser.moodle_compatible_formula(formula, item_id_map=item_id_map)
        return resolved_formula, unresolved, suggestions
