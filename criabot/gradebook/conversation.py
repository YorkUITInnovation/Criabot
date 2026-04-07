from __future__ import annotations

from .schemas import GradebookProposal, GradebookSessionRecord


class ConversationManager:
    def next_phase(self, session: GradebookSessionRecord, prompt: str) -> str:
        text = prompt.lower().strip()
        if session.phase == "INTAKE":
            return "ANALYSIS"
        if session.phase in {"ANALYSIS", "REFINEMENT"}:
            return "PROPOSAL"
        if session.phase == "PROPOSAL":
            if any(word in text for word in ("accept", "approved", "looks good", "proceed")):
                return "ACCEPTED"
            return "REFINEMENT"
        return session.phase

    def make_reply(self, session: GradebookSessionRecord, proposal: GradebookProposal | None) -> str:
        if session.phase == "INTAKE":
            return "Please provide syllabus details or upload references so I can analyze grading structure."
        if session.phase == "ANALYSIS":
            return "I am analyzing the syllabus and Moodle activities to build a gradebook proposal."
        if session.phase == "PROPOSAL" and proposal:
            category_list = ", ".join(f"{c.name} ({c.weight}%)" for c in proposal.categories)
            return f"Here is the current gradebook proposal: {category_list}. Tell me what to refine or say 'accept'."
        if session.phase == "REFINEMENT":
            return "I updated the proposal. Review the weights and mappings, then accept or request more changes."
        if session.phase == "ACCEPTED":
            return "Proposal accepted. The backend can now proceed to category creation and content mapping."
        return "Gradebook session updated."
