"""
Excel-style formula parser for Moodle gradebook calculations.

Supports formulas like:
    =[[assignment1]]+[[assignment2]]
    =round(average([[q1]],[[q2]]),2)
    =([[midterm]]*0.4)+([[final]]*0.6)

Single-bracket references (e.g., [hw1]) are accepted for backward compatibility,
then normalized to Moodle-compatible double-bracket refs.

@author     Kiarash Bashokian
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Dict, Optional


@dataclass
class FormulaValidationResult:
    """Result of formula validation."""
    is_valid: bool
    error_message: Optional[str] = None
    item_references: List[str] = None
    normalized_formula: str = ""


class FormulaParser:
    """Parse and validate Excel-style gradebook formulas."""

    # Pattern for item references: [[item_id]] (preferred) or [item_id] (legacy).
    ITEM_PATTERN = re.compile(r'\[\[([A-Za-z0-9_]+)\]\]|\[([A-Za-z0-9_]+)\]')

    # Empty references are explicitly invalid: [] or [[]].
    EMPTY_ITEM_PATTERN = re.compile(r'\[\[\s*\]\]|\[\s*\]')

    # Valid operators and characters in formulas (before item replacement).
    # Includes comparison operators to support expressions like if([[a]]>[[b]],...).
    VALID_CHARS_PATTERN = re.compile(r'^[0-9\s\+\-\*\/\(\)\[\]\.\,\;><=!a-zA-Z_]*$')

    # Supported Moodle-friendly function names (case-insensitive).
    ALLOWED_FUNCTIONS = {
        'sum', 'average', 'avg', 'max', 'min', 'if', 'round', 'mod', 'pi', 'power',
        'abs', 'sqrt', 'floor', 'ceil', 'ceiling',
    }

    @classmethod
    def parse_formula(cls, formula_text: str) -> FormulaValidationResult:
        """
        Parse and validate an Excel-style formula.

        Args:
            formula_text: Formula string, e.g., "=[hw1]*0.5 + [hw2]*0.5"

        Returns:
            FormulaValidationResult with validation status and details.
        """
        if not formula_text:
            return FormulaValidationResult(
                is_valid=False,
                error_message="Formula cannot be empty."
            )

        # Normalize: remove leading '=' if present
        formula = formula_text.strip()
        if formula.startswith('='):
            formula = formula[1:].strip()

        # Check for empty formula after removing '='
        if not formula:
            return FormulaValidationResult(
                is_valid=False,
                error_message="Formula cannot be empty after '='."
            )

        # Validate bracket matching
        bracket_error = cls._validate_bracket_matching(formula)
        if bracket_error:
            return FormulaValidationResult(
                is_valid=False,
                error_message=bracket_error
            )

        # Reject empty refs explicitly before extracting references.
        if cls.EMPTY_ITEM_PATTERN.search(formula):
            return FormulaValidationResult(
                is_valid=False,
                error_message="Formula contains an empty item reference. Use [[item_id]] (or [item_id])."
            )

        # Extract item references
        item_refs = cls._extract_item_references(formula)
        if not item_refs:
            return FormulaValidationResult(
                is_valid=False,
                error_message="Formula must contain at least one item reference (e.g., [[hw1]] or [[ID]])."
            )

        # Validate formula syntax
        syntax_error = cls._validate_syntax(formula)
        if syntax_error:
            return FormulaValidationResult(
                is_valid=False,
                error_message=syntax_error
            )

        return FormulaValidationResult(
            is_valid=True,
            error_message=None,
            item_references=item_refs,
            normalized_formula=cls._normalize_list_separators(formula)
        )

    @staticmethod
    def _normalize_list_separators(formula: str) -> str:
        """Normalize list separators to comma (YorkU default)."""
        return formula.replace(';', ',')

    @staticmethod
    def _validate_bracket_matching(formula: str) -> Optional[str]:
        """
        Validate that brackets in formula are properly matched.

        Returns:
            Error message if brackets are mismatched, None if valid.
        """
        depth = 0
        square_depth = 0

        for i, char in enumerate(formula):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth < 0:
                    return f"Mismatched closing parenthesis at position {i}."
            elif char == '[':
                square_depth += 1
            elif char == ']':
                square_depth -= 1
                if square_depth < 0:
                    return f"Mismatched closing bracket at position {i}."

        if depth > 0:
            return "Missing closing parenthesis in formula."
        if square_depth > 0:
            return "Missing closing bracket in formula."

        return None

    @staticmethod
    def _extract_item_references(formula: str) -> List[str]:
        """
        Extract item references from formula (e.g., [[hw1]], [ID]).

        Returns:
            List of unique item references found.
        """
        matches = FormulaParser.ITEM_PATTERN.findall(formula)
        # Return unique references preserving order
        seen = set()
        result = []
        for match in matches:
            ref = match[0] or match[1]
            if ref and ref not in seen:
                result.append(ref)
                seen.add(ref)
        return result

    @classmethod
    def _validate_syntax(cls, formula: str) -> Optional[str]:
        """
        Validate formula syntax (operators, structure, etc.).

        Returns:
            Error message if syntax is invalid, None if valid.
        """
        # First validate raw formula has allowed characters
        if not cls.VALID_CHARS_PATTERN.match(formula):
            return "Formula contains invalid characters. Use only numbers, operators (+, -, *, /), parentheses, and item references."

        # Replace item references with placeholder to check operator syntax
        temp_formula = re.sub(cls.ITEM_PATTERN, 'X', formula)

        # Validate function names used in function-call form (name(...)).
        for fn in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', temp_formula):
            if fn.lower() not in cls.ALLOWED_FUNCTIONS:
                return (
                    f"Unsupported function '{fn}'. Supported functions include: "
                    "sum, average, max, min, if, round, mod, pi, power."
                )

        # Check for consecutive operators
        if re.search(r'[\+\-\*\/]{2,}', temp_formula):
            return "Formula has consecutive operators (e.g., '++' or '*/')."

        # Check for leading/trailing operators
        stripped = temp_formula.strip()
        if stripped.startswith(('+', '*', '/')):
            return "Formula cannot start with an operator."
        if stripped.endswith(('+', '-', '*', '/')):
            return "Formula cannot end with an operator."

        # Check for division by zero literals (common error)
        if re.search(r'/\s*0(?![0-9])', temp_formula):
            return "Formula contains division by zero."

        return None

    @classmethod
    def moodle_compatible_formula(cls, formula: str, item_id_map: Optional[Dict[str, str]] = None) -> str:
        """
        Convert parsed formula to Moodle-compatible grade_item formula.

        If item_id_map is provided, replaces item names with their numeric IDs.
        Otherwise, keeps item references as-is (assuming they're already IDs or names).

        Args:
            formula: Original formula (with or without leading '=')
            item_id_map: Optional dict mapping item names to Moodle grade item IDs

        Returns:
            Moodle-compatible formula (e.g., "=[[1]]+[[2]]*0.5")
        """
        # Normalize formula
        text = formula.strip()
        if text.startswith('='):
            text = text[1:].strip()

        text = cls._normalize_list_separators(text)

        # If item_id_map provided, replace references with IDs
        if item_id_map:
            lowered_map = {str(k).lower(): str(v) for k, v in item_id_map.items()}

            def replace_ref(match):
                ref = match.group(1) or match.group(2)
                item_id = lowered_map.get(str(ref).lower(), ref)
                # Moodle uses double brackets: [[1]], [[2]], etc.
                return f'[[{item_id}]]'

            text = re.sub(cls.ITEM_PATTERN, replace_ref, text)
        else:
            # Normalize all legacy single-bracket refs to Moodle double-bracket refs.
            def normalize_ref(match):
                ref = match.group(1) or match.group(2)
                return f'[[{ref}]]'

            text = re.sub(cls.ITEM_PATTERN, normalize_ref, text)

        # Return with leading '=' for Moodle compatibility
        return f"={text}"

    @classmethod
    def extract_formula_and_detect(cls, text: str) -> Optional[Dict]:
        """
        Detect if text contains a formula instruction and extract it.

        Examples:
          "Set Final as [Midterm]*0.4 + [Final]*0.6" -> formula found
          "Use the formula =[hw1]+[hw2]" -> formula found
          "Set weights to 30%" -> no formula

        Returns:
            Dict with 'formula' and 'type' if formula detected, None otherwise.
        """
        text_lower = (text or '').lower()

        # Keywords suggesting formula instruction
        formula_keywords = (
            'formula', 'calculate', 'use this formula', 'equation',
            'calculation', 'compute', 'final grade',
        )

        # Check if text likely contains a formula instruction
        has_formula_keyword = any(kw in text_lower for kw in formula_keywords)
        has_formula_syntax = bool(re.search(r'\[\[?[A-Za-z0-9_]+\]?\]', text or ''))

        if not (has_formula_keyword or has_formula_syntax):
            return None

        # Extract formula patterns.
        formula_patterns = [
            r'(=\s*[0-9A-Za-z_\[\]\s\+\-\*\/\(\)\.,\;><=!]+)',
            r'(\[\[?[A-Za-z0-9_]+\]?\][0-9A-Za-z_\[\]\s\+\-\*\/\(\)\.,\;><=!]+)',
        ]

        extracted_formula = None
        for pattern in formula_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                extracted_formula = match.group(1).strip().rstrip('.')
                break

        if extracted_formula:
            return {
                'formula': extracted_formula,
                'type': 'user_formula',
                'source': 'chat',
                'original_text': text
            }

        return None
