from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import List, Dict, Optional

from .schemas import GradebookProposal, GradebookSessionRecord, GRADE_DISPLAY_TYPE_NAMES
from .proposal import ProposalGenerator, validate_proposal_weights
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

        # Explicit proposal requests should not be interpreted as acceptance.
        if self._looks_like_proposal_request(text):
            if session.phase in {"INTAKE", "ANALYSIS", "PROPOSAL", "REFINEMENT", "ACCEPTED", "COMPLETED"}:
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
    def _looks_like_gradebook_refinement(text: str) -> bool:
        """Return True if the prompt appears to contain a valid gradebook refinement request."""
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
        # Action verbs must be accompanied by gradebook context or numeric targets
        # to avoid false positives like "make me a pizza".
        if re.search(r"\b(?:set|make|change|adjust|update|increase|decrease|give|assign|replace|apply|use)\b", t):
            has_numeric_target = bool(re.search(r"\b\d+(?:\.\d+)?\s*%?\b", t))
            has_gradebook_context = bool(re.search(
                r"\b(?:assignments?|labs?|midterm|final(?:\s+exam)?|quizzes?|projects?|participation|category|categories|weight|weights|aggregation|method|formula|gradebook|drop|keep|hide|show|rename|split|subcategor(?:y|ies))\b",
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
            "normalize", "swap", "move", "to category",
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
            "- Set weights: 'Set Labs to 20%'\n"
            "- Add category: 'Add Quizzes' or 'Added Quizzes'\n"
            "- Add category with weight: 'Add Quizzes 10%'\n"
            "- Aggregation: 'Use weighted mean' or 'Use mean'\n"
            "- Splits: 'In Labs, split into Lab Reports 10%, In-Lab 5%'\n"
            "- Formula: 'Set Assignments formula to =average([[hw1]],[[hw2]])'\n"
            "- Visibility/rules: 'Hide Midterm until 2026-05-19' or 'Drop lowest 1 from Assignments'\n"
            "- Finalize: 'Accept proposal and generate mapping'\n"
            "- Undo/Redo: 'undo' or 'redo'\n\n"
            "Tip: type 'help' to see the full supported instruction list."
        )

    @staticmethod
    def _supported_instructions_help_text() -> str:
        return (
            "Here are supported instructions you can use:\n\n"
            "**Syllabus/Supporting docs**\n"
            "- 'Use my uploaded syllabus and give me a proposal'\n"
            "- 'Analyze the latest uploaded supporting document and regenerate proposal'\n"
            "- 'I uploaded a new file, use it and rebuild the gradebook proposal'\n\n"
            "**Weights and categories**\n"
            "- 'Set Assignments 35%, Labs 15%, Midterm 20%, Final 30%'\n"
            "- 'Change Labs to 20% and rebalance automatically'\n"
            "- 'Rename Labs to Laboratory'\n"
            "- 'Add Quizzes' — creates the category at 0% so you can rebalance later\n"
            "- 'Add Projects 15%' — creates the category with that weight immediately\n"
            "- 'Added Quizzes' — shorthand phrasing also works\n\n"
            "**Remove / drop a category**\n"
            "- 'Remove Quizzes' — frees the weight (total decreases; you can reallocate later)\n"
            "- 'Drop Labs evenly' — removes Labs and spreads its weight equally across remaining categories\n"
            "- 'Delete Midterm and give weight to Final Exam' — removes Midterm and adds its weight to Final Exam\n"
            "- 'Remove Assignments and split weight among Labs and Midterm' — removes Assignments and splits weight equally between the two targets\n\n"
            "**Aggregation method**\n"
            "- 'Use Weighted mean of grades'\n"
            "- 'Switch to Natural'\n"
            "- 'Show aggregation methods'\n\n"
            "**Excel-style formulas**\n"
            "- 'Set Assignments formula to =average([[hw1]],[[hw2]],[[project]])'\n"
            "- 'Use formula =([[midterm]]*0.4)+([[final]]*0.6) for Final Exam'\n"
            "- 'Clear formula from Labs'\n"
            "Use Moodle item references like `[[item_id]]` (legacy `[item]` is also accepted).\n"
            "Default separator is comma `,` (YorkU standard).\n\n"
            "If a formula is invalid, I'll return a direct warning and ask you to retry.\n"
            "Formula help: [YorkU custom formula guide](https://lthelp.yorku.ca/gradebook/creating-a-custom-formula)\n"
            "Excel help: [Excel formula reference](https://support.microsoft.com/excel)\n\n"
            "**Rules and visibility**\n"
            "- 'Drop lowest 1 from Assignments'\n"
            "- 'Keep highest 2 from Labs'\n"
            "- 'Hide Midterm until 2026-05-19'\n\n"
            "**Finalize flow**\n"
            "- 'Accept proposal and generate mapping'\n"
            "- 'Show mapping rows before finalize'\n"
            "- 'Finalize now'"
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

        # Handle questions: answer them without forcing phase transitions
        if text and self._looks_like_question(text):
            answer = self._answer_question(session, text, proposal)
            if answer:
                return answer

        # Uploaded/attached context should trigger analysis guidance, not unsupported warnings.
        if text and self._looks_like_upload_signal(text):
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

        if session.phase == "PROPOSAL" and proposal:
            # For formula parsing failures, return a focused warning instead of re-rendering full proposal.
            formula_requested = self._extract_formula_from_prompt(prompt) is not None
            proposal_changed = bool((session.extraction or {}).get("proposal_changed"))
            if formula_requested and proposal_changed:
                formula_error = self._formula_error_reply(proposal)
                if formula_error:
                    return formula_error

            categories_text = self._format_categories(proposal)
            total_weight = sum(cat.weight for cat in proposal.categories)
            effects_text = self._format_effects(proposal)
            notes_text = self._format_notes(session, proposal)
            findings_text = self._format_findings(session)

            aggregation_name = self._get_aggregation_method_name(proposal.aggregation_method)
            
            proposal_errors = validate_proposal_weights(proposal)
            has_weight_error = any(err.get("path") == ["proposal"] for err in proposal_errors)
            weight_warnings = check_weight_warnings(proposal)

            if has_weight_error:
                return (
                    f"Here is the gradebook structure I built based on your materials:\n\n"
                    f"{findings_text}{categories_text}\n**Total: {total_weight:.1f}%**{effects_text}{notes_text}\n\n"
                    "⚠ The total weight is not 100% yet. Please adjust the weights so they add up to 100% before we continue."
                )
            else:
                aggregation_msg = f"\n\n**Grade Aggregation Method**: {aggregation_name}\n(I'll use '{aggregation_name}' when creating your gradebook. If you prefer a different method, let me know.)"
                weight_msg = f"\n{weight_warnings[0]}" if weight_warnings else ""
                return (
                    f"Here is the gradebook structure based on what I found:\n\n"
                    f"{findings_text}{categories_text}\n**Total: {total_weight:.1f}%**{effects_text}{notes_text}"
                    f"{weight_msg}"
                    f"{aggregation_msg}\n\n"
                    "Does this look right? If you'd like to adjust any weights, categories, or the grade aggregation method, let me know and I'll refine it."
                )

        if session.phase == "REFINEMENT":
            if proposal:
                # For formula parsing failures, return a focused warning instead of re-rendering full proposal.
                formula_requested = self._extract_formula_from_prompt(prompt) is not None
                proposal_changed = bool((session.extraction or {}).get("proposal_changed"))
                if formula_requested and proposal_changed:
                    formula_error = self._formula_error_reply(proposal)
                    if formula_error:
                        return formula_error
                categories_text = self._format_categories(proposal)
                total_weight = sum(cat.weight for cat in proposal.categories)
                effects_text = self._format_effects(proposal)
                notes_text = self._format_notes(session, proposal)
                aggregation_name = self._get_aggregation_method_name(proposal.aggregation_method)
                proposal_errors = validate_proposal_weights(proposal)
                has_weight_error = any(err.get("path") == ["proposal"] for err in proposal_errors)
                weight_warnings = check_weight_warnings(proposal)
                if has_weight_error:
                    return (
                        f"Updated proposal:\n\n{categories_text}\n**Total: {total_weight:.1f}%**{effects_text}{notes_text}\n\n"
                        f"**Grade Aggregation Method**: {aggregation_name}\n\n"
                        "⚠ Total weight is still not 100%. Please adjust to proceed."
                    )
                else:
                    weight_msg = f"\n{weight_warnings[0]}" if weight_warnings else ""
                    return (
                        f"Updated proposal:\n\n{categories_text}\n**Total: {total_weight:.1f}%**{effects_text}{notes_text}\n"
                        f"{weight_msg}\n"
                        f"**Grade Aggregation Method**: {aggregation_name}\n\n"
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
                    "- `=([hw1]+[hw2]+[hw3])/3` — Average of three homeworks\n"
                    "- `=([midterm]*0.4)+([final]*0.6)` — Weighted average: 40% midterm, 60% final\n"
                    "- `=[quiz1]+[quiz2]*0.5` — Quiz 1 full, Quiz 2 half weight\n"
                    "- `=([lab]*0.5)+([project]*0.5)` — Split between lab and project\n\n"
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

    def _format_categories(self, proposal: GradebookProposal) -> str:
        """Format categories for display in conversation, including all per-category settings."""
        lines = []
        for category in proposal.categories:
            weight = category.weight
            item_count = len(category.items) if category.items else 0
            extra_label = " ★ extra credit" if getattr(category, 'extra_credit', False) else ""
            hidden_label = " 🔒 hidden" if getattr(category, 'hidden', False) else ""
            locked_label = " 🔐 locked" if getattr(category, 'locked', False) else ""
            lines.append(f"- **{category.name}** ({weight:.1f}%){extra_label}{hidden_label}{locked_label}: {item_count} items")

            # Aggregation rules
            settings = []
            drop_lowest = getattr(category, 'drop_lowest', 0)
            keep_highest = getattr(category, 'keep_highest', 0)
            aggregate_only_graded = getattr(category, 'aggregate_only_graded', True)
            aggregate_outcomes = getattr(category, 'aggregate_outcomes', False)
            effective_children = len(getattr(category, 'subcategories', None) or []) or len(category.items or [])
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

            # Grade total settings
            grade_min = getattr(category, 'grade_min', None)
            grade_max = getattr(category, 'grade_max', 100.0)
            grade_pass = getattr(category, 'grade_pass', None)
            hidden_until = getattr(category, 'hidden_until', None)
            lock_time = getattr(category, 'lock_time', None)
            display_type = getattr(category, 'display_type', 0)
            decimals = getattr(category, 'decimals', -1)
            if grade_min is not None:
                settings.append(f"min {grade_min:.0f} pts")
            if grade_max != 100.0:
                settings.append(f"max {grade_max:.0f} pts")
            if grade_pass is not None:
                settings.append(f"pass ≥ {grade_pass:.0f}")
            if hidden_until:
                hidden_label = datetime.fromtimestamp(int(hidden_until), tz=timezone.utc).strftime("%Y-%m-%d")
                settings.append(f"hidden until {hidden_label}")
            if lock_time:
                lock_label = datetime.fromtimestamp(int(lock_time), tz=timezone.utc).strftime("%Y-%m-%d")
                settings.append(f"lock at {lock_label}")
            if display_type != 0:
                settings.append(f"display: {GRADE_DISPLAY_TYPE_NAMES.get(display_type, str(display_type))}")
            if decimals >= 0:
                settings.append(f"{decimals} decimal{'s' if decimals != 1 else ''}")

            formula = getattr(category, 'calculation_formula', None)
            if formula:
                settings.append(f"formula: {formula}")

            if settings:
                lines.append(f"  ↳ {', '.join(settings)}")

            # Show first few items as examples
            if category.items and len(category.items) <= 3:
                for item in category.items[:3]:
                    lines.append(f"  • {item}")
            elif category.items and len(category.items) > 3:
                for item in category.items[:2]:
                    lines.append(f"  • {item}")
                lines.append(f"  • ... and {len(category.items) - 2} more")

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
            "Use Moodle-style double square brackets around item IDs: `[[item_id]]`.\n"
            "(Legacy single-bracket input like `[item]` is accepted and normalized.)\n\n"
            "**Examples:**\n"
            "- Simple average: `=average([[hw1]],[[hw2]],[[hw3]])`\n"
            "- Weighted calculation: `=([[midterm]]*0.3)+([[final]]*0.7)`\n"
            "- Nested functions: `=round(average([[q1]],[[q2]]),2)`\n"
            "- Conditional: `=if([[bonus]]>0, [[score]]+[[bonus]], [[score]])`\n\n"
            "**Supported operators:** `+` (addition), `-` (subtraction), `*` (multiplication), `/` (division)\n"
            "Use parentheses `()` for grouping operations. Use comma `,` between function arguments.\n\n"
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
