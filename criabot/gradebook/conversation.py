from __future__ import annotations

import re
from typing import List, Dict

from .schemas import GradebookProposal, GradebookSessionRecord


class ConversationManager:
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
            "give proposal",
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

    def next_phase(self, session: GradebookSessionRecord, prompt: str) -> str:
        text = prompt.lower().strip()
        extraction = session.extraction or {}
        has_syllabus = bool(extraction.get("has_syllabus"))

        # Check for acceptance keywords
        if self._looks_like_affirmation(text):
            if session.phase in ["PROPOSAL", "REFINEMENT"]:
                total = sum(cat.weight for cat in (session.proposal.categories if session.proposal else []))
                if abs(total - 100.0) <= 0.1:
                    return "ACCEPTED"
                else:
                    return session.phase  # Cannot accept if weights don't sum to 100%

        # Check for rejection/refinement keywords
        if self._looks_like_rejection(text):
            if session.phase == "PROPOSAL":
                return "REFINEMENT"
            return session.phase

        # Questions during flow: stay in current phase, respond naturally
        if self._looks_like_question(text):
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

        if session.phase in {"ANALYSIS", "REFINEMENT"}:
            return "PROPOSAL"

        if session.phase == "PROPOSAL":
            return "REFINEMENT"  # Default to refinement if unclear

        if session.phase in {"ACCEPTED", "COMPLETED"}:
            # New uploaded context should restart proposal workflow.
            if self._looks_like_context_signal(text):
                return "ANALYSIS"
            if self._looks_like_proposal_request(text):
                return "PROPOSAL"

        return session.phase

    def make_reply(self, session: GradebookSessionRecord, proposal: GradebookProposal | None, prompt: str = "") -> str:
        text = (prompt or "").lower().strip()
        extraction = session.extraction or {}
        has_syllabus = bool(extraction.get("has_syllabus"))

        # Handle questions: answer them without forcing phase transitions
        if text and self._looks_like_question(text):
            answer = self._answer_question(session, text, proposal)
            if answer:
                return answer

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

        if session.phase == "PROPOSAL" and proposal:
            categories_text = self._format_categories(proposal)
            total_weight = sum(cat.weight for cat in proposal.categories)
            notes_text = self._format_notes(session, proposal)
            findings_text = self._format_findings(session)
            
            if abs(total_weight - 100.0) > 0.1:
                return (
                    f"Here is the gradebook structure I built based on your materials:\n\n"
                    f"{findings_text}{categories_text}\n**Total: {total_weight:.1f}%**{notes_text}\n\n"
                    "⚠ The total weight is not 100% yet. Please adjust the weights so they add up to 100% before we continue."
                )
            else:
                return (
                    f"Here is the gradebook structure based on what I found:\n\n"
                    f"{findings_text}{categories_text}\n**Total: {total_weight:.1f}%**{notes_text}\n\n"
                    "Does this look right? If you'd like to adjust any weights or categories, let me know and I'll refine it."
                )

        if session.phase == "REFINEMENT":
            if proposal:
                categories_text = self._format_categories(proposal)
                total_weight = sum(cat.weight for cat in proposal.categories)
                notes_text = self._format_notes(session, proposal)
                if abs(total_weight - 100.0) > 0.1:
                    return (
                        f"Updated proposal:\n\n{categories_text}\n**Total: {total_weight:.1f}%**{notes_text}\n\n"
                        "⚠ Total weight is still not 100%. Please adjust to proceed."
                    )
                else:
                    return (
                        f"Updated proposal:\n\n{categories_text}\n**Total: {total_weight:.1f}%**{notes_text}\n\n"
                        "What else would you like to adjust? I can modify weights, add/remove categories, or rename items."
                    )
            else:
                return (
                    "I'm ready to refine your gradebook. "
                    "What would you like to change? You can adjust weights, add/remove categories, or reorganize items."
                )

        if session.phase == "ACCEPTED":
            return (
                "✓ Gradebook proposal accepted! "
                "I'm now mapping your course activities to the grade categories. "
                "Once complete, you'll review the mapping before I finalize everything in Moodle."
            )

        if session.phase == "COMPLETED":
            return (
                "✓ Gradebook finalized successfully! "
                "All course activities have been mapped and are now ready in Moodle. "
                "You can download a summary or make adjustments directly in Moodle."
            )

        return "Gradebook session updated. How can I help?"

    def _answer_question(self, session: GradebookSessionRecord, question_text: str, proposal: GradebookProposal | None) -> str:
        """Generate contextual answers to common gradebook questions."""
        q = question_text.lower()
        extraction = session.extraction or {}

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
            return "You can add or remove categories anytime by describing them. Say something like: 'Add a Projects category with 15%' or 'Remove the Labs category and redistribute to Assignments.'"

        # Questions about uploads
        if "upload" in q or "file" in q:
            return "You can upload your syllabus or grading policy as a PDF or Word document. I'll extract the grading structure and use it to build your gradebook proposal."

        # Questions about Moodle mapping
        if "mapp" in q or "activity" in q:
            return "Once we finalize your gradebook structure, I'll automatically map your Moodle course activities (assignments, quizzes, etc.) to the categories. You can review and adjust before finalizing."

        # Default: no specific answer
        return ""

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

    def _format_notes(self, session: GradebookSessionRecord, proposal: GradebookProposal) -> str:
        issues = self.validate_weights(proposal)
        notes = list(proposal.notes or [])
        conflict_notes = self._syllabus_conflict_warnings(session, proposal)
        if not issues and not notes and not conflict_notes:
            return ""

        lines = ["", "", "**Checks:**"]
        for issue in issues:
            lines.append(f"- {issue}")
        for note in notes[-3:]:
            lines.append(f"- {note}")
        for note in conflict_notes:
            lines.append(f"- {note}")
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
