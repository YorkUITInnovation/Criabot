from __future__ import annotations

import difflib
import json
import os
import re
from typing import List, Dict, Optional, Tuple

from .schemas import CourseActivity, GradebookCategory, GradebookProposal


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
    }
    CATEGORY_ALIAS_PATTERNS = (
        (re.compile(r"\b(assign(?:ment)?s?|home\s*work|hw)\b", flags=re.IGNORECASE), "Assignments"),
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
        for activity in activities:
            activity_name = activity.name.lower()
            if "lab" in activity_name:
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
                "Initial proposal is generated from syllabus + available Moodle activities.",
                "Professor can refine category names, weights, and mapping before acceptance.",
            ],
        )

    # Notes that are auto-generated (not user-visible descriptive text) get stripped each turn.
    _EPHEMERAL_NOTE_PREFIXES = (
        "weight check:",
        "auto-normalized",
        "removed category",
    )

    @staticmethod
    def _is_ephemeral_note(note: str) -> bool:
        low = note.lower()
        return any(low.startswith(prefix) for prefix in ProposalGenerator._EPHEMERAL_NOTE_PREFIXES)

    def update_from_prompt(self, proposal: GradebookProposal, prompt: str) -> GradebookProposal:
        updated = GradebookProposal.parse_obj(proposal.model_dump())
        prompt_lower = prompt.lower()

        # Strip ephemeral per-turn notes so they don't accumulate.
        updated.notes = [n for n in (updated.notes or []) if not self._is_ephemeral_note(n)]

        # Detect split context early so weight extraction skips subcategory names.
        is_split_prompt = bool(re.search(r"\bsplit\b.+\binto\b", prompt_lower))

        self._apply_removals(updated, prompt)

        weights = self._parse_weight_assignments(updated, prompt, skip_split_pieces=is_split_prompt)
        if weights:
            self._apply_weight_updates(updated, weights)

        if is_split_prompt:
            split_parts = self._parse_split_categories(prompt)
            self._record_split_request(updated, split_parts)

        self._post_update_checks(updated, prompt)

        return updated

    # Collect names of split subcategories so they are excluded from weight-parsing
    # (prevents "Homework 15%" being misread as "Assignments 15%" via alias).
    @staticmethod
    def _extract_split_piece_names(prompt: str) -> List[str]:
        match = re.search(r"\bsplit\b.+?\binto\b(.+)", prompt, flags=re.IGNORECASE)
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
            # Also strip everything after "split ... into" to avoid parsing subcategory weights.
            clean_prompt = re.sub(r"\bsplit\b.+", "", clean_prompt, flags=re.IGNORECASE).strip()

        pattern = re.compile(
            r"(?:set|make|change|adjust|update|keep|use)?\s*([a-zA-Z][a-zA-Z ]{1,40}?)\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\s*%",
            flags=re.IGNORECASE,
        )
        matches = pattern.findall(clean_prompt) + re.findall(
            r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s*(\d+(?:\.\d+)?)\s*%",
            clean_prompt,
            flags=re.IGNORECASE,
        )
        weights: Dict[str, float] = {}
        for raw_name, raw_weight in matches:
            if skip_split_pieces and raw_name.strip().lower() in split_piece_names:
                continue
            resolved = self._resolve_category_name(proposal, raw_name)
            if resolved is None:
                continue
            # Last occurrence wins for the same category (handles repeat mentions).
            weights[resolved] = float(raw_weight)
        return weights

    def _apply_weight_updates(self, proposal: GradebookProposal, weights: Dict[str, float]) -> None:
        normalized_map = {cat.name.lower(): cat for cat in proposal.categories}
        for name, weight in weights.items():
            key = name.lower()
            if key in normalized_map:
                normalized_map[key].weight = weight

    def _parse_split_categories(self, prompt: str) -> List[Tuple[str, Optional[float]]]:
        # Match "split <anything> into <parts>" generically.
        match = re.search(r"\bsplit\b.+?\binto\b\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            return []

        # Remove trailing qualifications like "but keep Assignments as the parent category".
        raw_list = re.sub(r"\bbut\b.+", "", match.group(1), flags=re.IGNORECASE).strip()
        pieces = re.split(r",| and | & ", raw_list)
        parsed: List[Tuple[str, Optional[float]]] = []
        for piece in pieces:
            cleaned = piece.strip()
            if not cleaned:
                continue
            weighted = re.match(r"([a-zA-Z ]+?)\s*(\d+(?:\.\d+)?)\s*%", cleaned, flags=re.IGNORECASE)
            if weighted:
                parsed.append((weighted.group(1).strip().title(), float(weighted.group(2))))
            elif re.search(r"[a-zA-Z]", cleaned):
                parsed.append((re.sub(r"\d+(?:\.\d+)?\s*%", "", cleaned).strip().title(), None))
        return parsed

    def _record_split_request(self, proposal: GradebookProposal, split_parts: List[Tuple[str, Optional[float]]]) -> None:
        if not split_parts:
            return

        labels = []
        for name, weight in split_parts:
            if weight is None:
                labels.append(name)
            else:
                labels.append(f"{name} {weight:.1f}%")

        proposal.notes.append(
            "Assignments split requested (treated as internal allocation): " + ", ".join(labels) + "."
        )

    def _apply_removals(self, proposal: GradebookProposal, prompt: str) -> None:
        removals = re.findall(r"\b(?:remove|delete)\s+([a-zA-Z][a-zA-Z ]{1,40})\b", prompt, flags=re.IGNORECASE)
        if not removals:
            return

        for raw_name in removals:
            resolved = self._resolve_category_name(proposal, raw_name)
            if not resolved:
                continue
            removable = next((cat for cat in proposal.categories if cat.name.lower() == resolved.lower()), None)
            if removable is None:
                continue
            removed_weight = removable.weight
            proposal.categories = [cat for cat in proposal.categories if cat is not removable]
            if proposal.categories:
                share = removed_weight / len(proposal.categories)
                for cat in proposal.categories:
                    cat.weight += share
            proposal.notes.append(f"Removed category '{resolved}' and redistributed {removed_weight:.1f}% across remaining categories.")

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
            proposal.notes.append("Auto-normalized category weights to 100%.")

        if abs(total - 100.0) > 0.1:
            proposal.notes.append(
                f"Weight check: total is {total:.1f}% (expected 100%)."
            )

    @staticmethod
    def _normalize_name(value: str) -> str:
        cleaned = value.lower()
        cleaned = re.sub(r"\b(set|make|change|adjust|update|keep|with|to|is|are|exactly|total|category|categories)\b", " ", cleaned)
        cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
