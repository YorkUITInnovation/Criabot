import pytest
from criabot.gradebook.formula_parser import FormulaParser, FormulaValidationResult


class TestFormulaParser:
    """Test suite for FormulaParser."""

    def test_simple_formula_with_equal_sign(self):
        """Test parsing formula with leading equals sign."""
        result = FormulaParser.parse_formula("=[[hw1]]+[[hw2]]")
        assert result.is_valid
        assert result.item_references == ["hw1", "hw2"]
        assert result.normalized_formula == "[[hw1]]+[[hw2]]"

    def test_simple_formula_without_equal_sign(self):
        """Test parsing formula without leading equals sign."""
        result = FormulaParser.parse_formula("[[hw1]]+[[hw2]]")
        assert result.is_valid
        assert result.item_references == ["hw1", "hw2"]
        assert result.normalized_formula == "[[hw1]]+[[hw2]]"

    def test_weighted_formula(self):
        """Test parsing weighted average formula."""
        result = FormulaParser.parse_formula("=([[midterm]]*0.3)+([[final]]*0.7)")
        assert result.is_valid
        assert sorted(result.item_references) == ["final", "midterm"]
        assert "midterm" in result.normalized_formula
        assert "final" in result.normalized_formula

    def test_complex_formula(self):
        """Test parsing complex formula with multiple items and operations."""
        result = FormulaParser.parse_formula("=([lab1]*0.1)+([lab2]*0.15)+([exam]*0.75)")
        assert result.is_valid
        assert len(result.item_references) == 3
        assert "lab1" in result.item_references
        assert "lab2" in result.item_references
        assert "exam" in result.item_references

    def test_formula_with_spaces(self):
        """Test parsing formula with extra spaces."""
        result = FormulaParser.parse_formula("= ( [[midterm]] * 0.4 ) + ( [[final]] * 0.6 )")
        assert result.is_valid
        assert result.item_references == ["midterm", "final"]

    def test_nested_function_formula(self):
        """Test parsing nested functions with Moodle-style refs."""
        result = FormulaParser.parse_formula("=round(average([[q1]],[[q2]]),2)")
        assert result.is_valid
        assert result.item_references == ["q1", "q2"]

    def test_empty_formula_raises_error(self):
        """Test that empty formula raises error."""
        result = FormulaParser.parse_formula("")
        assert not result.is_valid
        assert "empty" in result.error_message.lower()

    def test_only_equal_sign_raises_error(self):
        """Test that only '=' raises error."""
        result = FormulaParser.parse_formula("=")
        assert not result.is_valid
        assert "empty" in result.error_message.lower()

    def test_mismatched_brackets_missing_close(self):
        """Test error detection for missing closing bracket."""
        result = FormulaParser.parse_formula("=[hw1]+[hw2")
        assert not result.is_valid
        assert "bracket" in result.error_message.lower()
        assert "closing" in result.error_message.lower()

    def test_mismatched_brackets_extra_close(self):
        """Test error detection for extra closing bracket."""
        result = FormulaParser.parse_formula("=[hw1]+[hw2]]")
        assert not result.is_valid
        assert "bracket" in result.error_message.lower()

    def test_mismatched_parentheses_missing_close(self):
        """Test error detection for missing closing parenthesis."""
        result = FormulaParser.parse_formula("=([hw1]+[hw2]")
        assert not result.is_valid
        assert "parenthesis" in result.error_message.lower()

    def test_mismatched_parentheses_extra_close(self):
        """Test error detection for extra closing parenthesis."""
        result = FormulaParser.parse_formula("=([hw1]+[hw2]))")
        assert not result.is_valid
        assert "parenthesis" in result.error_message.lower()

    def test_no_item_references_raises_error(self):
        """Test that formula without items raises error."""
        result = FormulaParser.parse_formula("=1+2+3")
        assert not result.is_valid
        assert "item reference" in result.error_message.lower()

    def test_empty_item_reference_rejected(self):
        """Test that empty references [] or [[]] are rejected."""
        result = FormulaParser.parse_formula("=average([[]],[[q1]])")
        assert not result.is_valid
        assert "empty item reference" in result.error_message.lower()

    def test_consecutive_operators_raises_error(self):
        """Test error detection for consecutive operators."""
        result = FormulaParser.parse_formula("=[hw1]++[hw2]")
        assert not result.is_valid
        assert "consecutive" in result.error_message.lower() or "operator" in result.error_message.lower()

    def test_leading_operator_raises_error(self):
        """Test error detection for leading operator."""
        result = FormulaParser.parse_formula("=+[hw1]+[hw2]")
        assert not result.is_valid
        assert "start" in result.error_message.lower() or "operator" in result.error_message.lower()

    def test_trailing_operator_raises_error(self):
        """Test error detection for trailing operator."""
        result = FormulaParser.parse_formula("=[hw1]+[hw2]+")
        assert not result.is_valid
        assert "end" in result.error_message.lower() or "operator" in result.error_message.lower()

    def test_division_by_zero_raises_error(self):
        """Test error detection for division by zero."""
        result = FormulaParser.parse_formula("=[hw1]/0")
        assert not result.is_valid
        assert "division by zero" in result.error_message.lower()

    def test_invalid_characters_raise_error(self):
        """Test error detection for invalid characters."""
        result = FormulaParser.parse_formula("=[hw1]+[hw2]&[hw3]")
        assert not result.is_valid
        assert "invalid" in result.error_message.lower() or "character" in result.error_message.lower()

    def test_duplicate_item_references_deduplicated(self):
        """Test that duplicate item references are deduplicated."""
        result = FormulaParser.parse_formula("=([[hw1]]+[[hw1]])/2")
        assert result.is_valid
        assert result.item_references == ["hw1"]

    def test_extract_formula_from_chat_prompt_with_equals(self):
        """Test extracting formula from natural language chat prompt."""
        text = "Set the final grade using this formula: =[[midterm]]*0.4 + [[final]]*0.6"
        result = FormulaParser.extract_formula_and_detect(text)
        assert result is not None
        assert "formula" in result
        assert "midterm" in result["formula"].lower() or "0.4" in result["formula"]

    def test_extract_formula_from_chat_prompt_set_as(self):
        """Test extracting formula using 'set as' phrasing."""
        text = "Set Final as [[Midterm]]*0.4 + [[Final]]*0.6"
        result = FormulaParser.extract_formula_and_detect(text)
        assert result is not None

    def test_extract_formula_and_detect_keeps_full_parenthesized_expression(self):
        """Regression: extraction should not truncate after the first closing bracket."""
        text = "Set Final Exam as =([[midterm]]*0.4)+([[final]]*0.6)"
        result = FormulaParser.extract_formula_and_detect(text)

        assert result is not None
        assert result['formula'] == "=([[midterm]]*0.4)+([[final]]*0.6)"

    def test_no_formula_in_non_formula_text(self):
        """Test that non-formula text returns None."""
        text = "Set weights to 30% for assignments"
        result = FormulaParser.extract_formula_and_detect(text)
        assert result is None

    def test_moodle_compatible_formula_with_ids(self):
        """Test converting formula to Moodle-compatible format."""
        formula = "=[hw1]+[hw2]"
        item_map = {"hw1": "1", "hw2": "2"}
        result = FormulaParser.moodle_compatible_formula(formula, item_map)
        assert result.startswith("=")
        assert "[[1]]" in result
        assert "[[2]]" in result

    def test_moodle_compatible_formula_without_map(self):
        """Test Moodle format without ID mapping."""
        formula = "=[hw1]+[hw2]"
        result = FormulaParser.moodle_compatible_formula(formula)
        assert result.startswith("=")
        assert "[[hw1]]" in result
        assert "[[hw2]]" in result

    def test_moodle_compatible_formula_normalizes_semicolon_separator(self):
        """Test semicolon separators normalize to comma (YorkU default)."""
        formula = "=average([[q1]];[[q2]])"
        result = FormulaParser.moodle_compatible_formula(formula)
        assert result == "=average([[q1]],[[q2]])"

    def test_formula_with_numeric_ids(self):
        """Test formula using numeric item IDs."""
        result = FormulaParser.parse_formula("=([[1]]*0.5)+([[2]]*0.5)")
        assert result.is_valid
        assert "1" in result.item_references
        assert "2" in result.item_references

    def test_formula_with_underscores_in_names(self):
        """Test formula with underscores in item names."""
        result = FormulaParser.parse_formula("=[[hw_1]]+[[hw_2]]+[[hw_3]]")
        assert result.is_valid
        assert "hw_1" in result.item_references
        assert "hw_2" in result.item_references
        assert "hw_3" in result.item_references

    def test_complex_nested_parentheses(self):
        """Test complex formula with nested parentheses."""
        result = FormulaParser.parse_formula("=(([[lab1]]*0.5)+([[lab2]]*0.5))*0.3+([[exam]]*0.7)")
        assert result.is_valid
        assert len(result.item_references) == 3
