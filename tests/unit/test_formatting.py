"""Unit tests for text formatting utilities."""

import pytest

from src.utils.formatters.base import (
    _parse_inline_elements,
    flatten_text,
    text_to_rich_text_blocks,
)
from src.utils.slack_helpers import _rich_text_to_plain_text


class TestFlattenText:
    """Tests for flatten_text function."""

    def test_preserves_flat_bullet_list(self):
        text = "- Item A\n- Item B\n- Item C"
        result = flatten_text(text)
        assert result == "- Item A\n- Item B\n- Item C"

    def test_preserves_flat_numbered_list(self):
        text = "1. First\n2. Second\n3. Third"
        result = flatten_text(text)
        assert result == "1. First\n2. Second\n3. Third"

    def test_joins_continuation_lines(self):
        text = "1. First item\n   continues here\n2. Second item"
        result = flatten_text(text)
        assert result == "1. First item continues here\n2. Second item"

    def test_preserves_indentation_for_sub_bullets(self):
        text = "1. First item\n   - Sub bullet A\n   - Sub bullet B\n2. Second item"
        result = flatten_text(text)
        lines = result.split("\n")
        # Sub-bullets should have leading whitespace preserved
        assert lines[1].startswith("   - ")
        assert lines[2].startswith("   - ")
        # Parent items should not have indentation
        assert lines[0] == "1. First item"
        assert lines[3] == "2. Second item"

    def test_preserves_indentation_for_nested_bullets(self):
        text = "- Main item\n  - Sub item\n  - Another sub\n- Second main"
        result = flatten_text(text)
        lines = result.split("\n")
        assert lines[1].startswith("  - ")
        assert lines[2].startswith("  - ")


class TestParseInlineElements:
    """Tests for _parse_inline_elements function."""

    def test_bold(self):
        result = _parse_inline_elements("**bold text**")
        assert len(result) == 1
        assert result[0]["text"] == "bold text"
        assert result[0]["style"]["bold"] is True

    def test_italic_underscores(self):
        result = _parse_inline_elements("_italic text_")
        assert len(result) == 1
        assert result[0]["text"] == "italic text"
        assert result[0]["style"]["italic"] is True

    def test_italic_underscores_in_sentence(self):
        result = _parse_inline_elements("before _italic_ after")
        assert len(result) == 3
        assert result[1]["text"] == "italic"
        assert result[1]["style"]["italic"] is True

    def test_snake_case_not_italic(self):
        """Underscores in snake_case identifiers should not be treated as italic."""
        result = _parse_inline_elements("snake_case_name")
        assert len(result) == 1
        assert result[0]["text"] == "snake_case_name"
        assert "style" not in result[0] or not result[0].get("style", {}).get("italic")

    def test_leading_underscore_identifier_not_italic(self):
        """Leading underscore identifiers like _get_us_market_holidays are not italic."""
        result = _parse_inline_elements("_get_us_market_holidays")
        assert len(result) == 1
        assert result[0]["text"] == "_get_us_market_holidays"

    def test_all_caps_with_underscore_not_italic(self):
        """UPPER_CASE constants should not be treated as italic."""
        result = _parse_inline_elements("AFTER_HOURS")
        assert len(result) == 1
        assert result[0]["text"] == "AFTER_HOURS"

    def test_inline_code(self):
        result = _parse_inline_elements("`code_with_underscores`")
        assert len(result) == 1
        assert result[0]["text"] == "code_with_underscores"
        assert result[0]["style"]["code"] is True

    def test_multiple_italic_sections(self):
        result = _parse_inline_elements("_italic_ and _also italic_")
        italic_parts = [e for e in result if e.get("style", {}).get("italic")]
        assert len(italic_parts) == 2
        assert italic_parts[0]["text"] == "italic"
        assert italic_parts[1]["text"] == "also italic"


class TestTextToRichTextBlocks:
    """Tests for text_to_rich_text_blocks function."""

    def test_flat_bullet_list(self):
        text = "- Item A\n- Item B\n- Item C"
        blocks = text_to_rich_text_blocks(text)
        elems = blocks[0]["elements"]
        assert len(elems) == 1
        assert elems[0]["type"] == "rich_text_list"
        assert elems[0]["style"] == "bullet"
        assert len(elems[0]["elements"]) == 3
        assert "indent" not in elems[0]

    def test_flat_numbered_list(self):
        text = "1. First\n2. Second\n3. Third"
        blocks = text_to_rich_text_blocks(text)
        elems = blocks[0]["elements"]
        assert len(elems) == 1
        assert elems[0]["type"] == "rich_text_list"
        assert elems[0]["style"] == "ordered"
        assert len(elems[0]["elements"]) == 3
        assert "indent" not in elems[0]

    def test_numbered_with_indented_sub_bullets(self):
        text = "1. First item\n   - Sub A\n   - Sub B\n2. Second item\n   - Sub C"
        blocks = text_to_rich_text_blocks(text)
        elems = blocks[0]["elements"]
        # Should produce: ordered(1 item), bullet(2 items, indent=1),
        #                  ordered(1 item), bullet(1 item, indent=1)
        assert len(elems) == 4
        assert elems[0]["style"] == "ordered"
        assert "indent" not in elems[0]
        assert elems[1]["style"] == "bullet"
        assert elems[1]["indent"] == 1
        assert len(elems[1]["elements"]) == 2
        assert elems[2]["style"] == "ordered"
        assert elems[3]["style"] == "bullet"
        assert elems[3]["indent"] == 1

    def test_nested_bullets(self):
        text = "- Main\n  - Sub\n- Another main"
        blocks = text_to_rich_text_blocks(text)
        elems = blocks[0]["elements"]
        assert len(elems) == 3
        assert elems[0]["style"] == "bullet"
        assert "indent" not in elems[0]
        assert elems[1]["style"] == "bullet"
        assert elems[1]["indent"] == 1
        assert elems[2]["style"] == "bullet"
        assert "indent" not in elems[2]

    def test_identifiers_with_underscores_in_list(self):
        text = "1. _nth_weekday_of_month test\n   - Fix _get_us_market_holidays import"
        blocks = text_to_rich_text_blocks(text)
        elems = blocks[0]["elements"]
        # Check numbered item text is not mangled by italic parsing
        numbered_text = "".join(
            e["text"] for e in elems[0]["elements"][0]["elements"]
        )
        assert "_nth_weekday_of_month" in numbered_text
        # Check bullet sub-item text
        bullet_text = "".join(
            e["text"] for e in elems[1]["elements"][0]["elements"]
        )
        assert "_get_us_market_holidays" in bullet_text


class TestRichTextToPlainText:
    """Tests for _rich_text_to_plain_text fallback."""

    def test_nested_list_fallback(self):
        """Indented lists should render with leading spaces in fallback."""
        rich_text_block = {
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "ordered",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "First item"}],
                        }
                    ],
                },
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 1,
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "Sub bullet"}],
                        }
                    ],
                },
            ]
        }
        result = _rich_text_to_plain_text(rich_text_block)
        assert "1. First item" in result
        assert "    • Sub bullet" in result

    def test_flat_list_no_indent(self):
        """Non-indented lists should not have leading spaces."""
        rich_text_block = {
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "Item A"}],
                        },
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "Item B"}],
                        },
                    ],
                }
            ]
        }
        result = _rich_text_to_plain_text(rich_text_block)
        assert "\n• Item A" in result
        assert "\n• Item B" in result
        assert "    •" not in result
