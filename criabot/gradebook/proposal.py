from __future__ import annotations

import re
from typing import List, Dict, Optional

from .schemas import CourseActivity, GradebookCategory, GradebookProposal


class ProposalGenerator:
    DEFAULT_CATEGORIES = [
        ("Assignments", 25.0),
        ("Labs", 15.0),
        ("Midterm", 30.0),
        ("Final Exam", 30.0),
    ]

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

    def update_from_prompt(self, proposal: GradebookProposal, prompt: str) -> GradebookProposal:
        updated = GradebookProposal.parse_obj(proposal.model_dump())
        prompt_lower = prompt.lower()

        weights = self._parse_weight_assignments(prompt)
        if weights:
            self._apply_weight_updates(updated, weights)

        if "split assignments into" in prompt_lower:
            split_names = self._parse_split_categories(prompt_lower)
            if split_names:
                self._split_assignments(updated, split_names)

        return updated

    def _parse_weight_assignments(self, prompt: str) -> Dict[str, float]:
        pattern = re.compile(r"([a-zA-Z ]+?)\s*(?:is|are|to|:)?\s*(\d+(?:\.\d+)?)\s*%")
        matches = pattern.findall(prompt)
        weights: Dict[str, float] = {}
        for raw_name, raw_weight in matches:
            normalized_name = raw_name.strip().lower()
            for prefix in ("set ", "make ", "change "):
                if normalized_name.startswith(prefix):
                    normalized_name = normalized_name[len(prefix):].strip()
                    break
            weights[normalized_name] = float(raw_weight)
        return weights

    def _apply_weight_updates(self, proposal: GradebookProposal, weights: Dict[str, float]) -> None:
        normalized_map = {cat.name.lower(): cat for cat in proposal.categories}
        for name, weight in weights.items():
            if name in normalized_map:
                normalized_map[name].weight = weight
            else:
                proposal.categories.append(GradebookCategory(name=name.title(), weight=weight, items=[]))

    def _parse_split_categories(self, prompt: str) -> List[str]:
        match = re.search(r"split assignments into (.+)", prompt)
        if not match:
            return []

        pieces = re.split(r",| and | & ", match.group(1))
        return [piece.strip().title() for piece in pieces if piece.strip()]

    def _split_assignments(self, proposal: GradebookProposal, new_categories: List[str]) -> None:
        assignment_category = next((cat for cat in proposal.categories if cat.name.lower() == "assignments"), None)
        if not assignment_category or not assignment_category.items:
            return

        split_items = assignment_category.items
        total_weight = assignment_category.weight
        split_weight = total_weight / max(len(new_categories), 1)

        # Remove the original assignments category and create new categories
        proposal.categories = [cat for cat in proposal.categories if cat is not assignment_category]
        item_chunks = [split_items[i::len(new_categories)] for i in range(len(new_categories))]
        for name, items in zip(new_categories, item_chunks):
            proposal.categories.append(GradebookCategory(name=name, weight=split_weight, items=items))
