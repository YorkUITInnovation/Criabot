from __future__ import annotations

import re
from typing import List

from .schemas import GradebookProposal, GradebookSessionRecord


class ConversationManager:
    def next_phase(self, session: GradebookSessionRecord, prompt: str) -> str:
        text = prompt.lower().strip()

        # Check for acceptance keywords
        if any(word in text for word in ("accept", "approved", "looks good", "proceed", "yes", "fine", "good")):
            if session.phase in ["PROPOSAL", "REFINEMENT"]:
                return "ACCEPTED"
            return session.phase

        # Check for rejection/refinement keywords
        if any(word in text for word in ("change", "modify", "adjust", "different", "no", "refine", "revise")):
            if session.phase == "PROPOSAL":
                return "REFINEMENT"
            return session.phase

        # Phase transitions based on current state
        if session.phase == "INTAKE":
            return "ANALYSIS"
        if session.phase in {"ANALYSIS", "REFINEMENT"}:
            return "PROPOSAL"
        if session.phase == "PROPOSAL":
            return "REFINEMENT"  # Default to refinement if unclear

        return session.phase

    def make_reply(self, session: GradebookSessionRecord, proposal: GradebookProposal | None) -> str:
        if session.phase == "INTAKE":
            return "I couldn't find a syllabus in your course materials. Could you upload your syllabus or describe your grading breakdown (e.g., 'Assignments 40%, Midterm 30%, Final 30%') so I can help build your gradebook?"

        if session.phase == "ANALYSIS":
            return "I've analyzed your course materials and Moodle activities. Let me create an initial gradebook proposal based on what I found."

        if session.phase == "PROPOSAL" and proposal:
            categories_text = self._format_categories(proposal)
            total_weight = sum(cat.weight for cat in proposal.categories)
            return f"Here's my proposed gradebook structure:\n\n{categories_text}\n**Total: {total_weight:.1f}%**\n\nWould you like to accept this proposal, or would you like me to make any changes?"

        if session.phase == "REFINEMENT":
            if proposal:
                categories_text = self._format_categories(proposal)
                total_weight = sum(cat.weight for cat in proposal.categories)
                return f"Here's the updated proposal:\n\n{categories_text}\n**Total: {total_weight:.1f}%**\n\nPlease let me know what you'd like to adjust (weights, categories, or item assignments)."
            else:
                return "I need more information about your grading preferences. Could you tell me how many categories you want and their approximate weights?"

        if session.phase == "ACCEPTED":
            return "Gradebook proposal accepted! The system will now create the grade categories in Moodle and map your course content to them. You'll be able to review and adjust the content mapping before it's finalized."

        return "Gradebook session updated. How can I help you refine your gradebook structure?"

    def _format_categories(self, proposal: GradebookProposal) -> str:
        """Format categories for display in conversation"""
        lines = []
        for category in proposal.categories:
            weight = category.weight
            item_count = len(category.items) if category.items else 0
            lines.append(f"- **{category.name}** ({weight:.1f}%): {item_count} items")

            # Show first few items as examples
            if category.items and len(category.items) <= 3:
                for item in category.items[:3]:
                    lines.append(f"  • {item}")
            elif category.items and len(category.items) > 3:
                for item in category.items[:2]:
                    lines.append(f"  • {item}")
                lines.append(f"  • ... and {len(category.items) - 2} more")

        return "\n".join(lines)

    def validate_weights(self, proposal: GradebookProposal) -> List[str]:
        """Validate that proposal weights are reasonable"""
        issues = []
        total_weight = sum(cat.weight for cat in proposal.categories)

        if abs(total_weight - 100.0) > 0.1:
            issues.append(f"Total weight is {total_weight:.1f}%, should be 100%")

        for category in proposal.categories:
            if category.weight <= 0:
                issues.append(f"Category '{category.name}' has invalid weight {category.weight}")
            if category.weight > 100:
                issues.append(f"Category '{category.name}' weight {category.weight}% seems too high")

        return issues
