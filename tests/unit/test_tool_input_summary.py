"""Unit tests for tool input summary helpers."""

from types import SimpleNamespace

from src.utils.tool_input_summary import (
    _first_present,
    format_tool_input_summary,
    truncate_cmd,
    truncate_path,
)


def _display() -> SimpleNamespace:
    return SimpleNamespace(
        truncate_path_length=16,
        truncate_cmd_length=18,
        truncate_pattern_length=10,
        truncate_text_length=12,
        truncate_url_length=14,
    )


def test_format_tool_input_summary_handles_simple_truncation_rules() -> None:
    """Path, cmd, pattern, text, and url rules should use the configured truncation lengths."""
    rules = {
        "open_file": {"type": "path", "keys": ["path"]},
        "run_command": {"type": "cmd", "keys": ["cmd"]},
        "search": {"type": "pattern", "keys": ["pattern"]},
        "write": {"type": "text", "keys": ["text"]},
        "browse": {"type": "url", "keys": ["url"]},
    }
    display = _display()

    assert format_tool_input_summary(
        "open_file",
        {"path": "/very/long/path/to/example.txt"},
        display,
        rules,
    ) == "`...o/example.txt`"
    assert format_tool_input_summary(
        "run_command",
        {"cmd": "echo hello\nworld from codex"},
        display,
        rules,
    ) == "`echo hello worl...`"
    assert format_tool_input_summary(
        "search",
        {"pattern": "0123456789ABC"},
        display,
        rules,
    ) == "`0123456789...`"
    assert format_tool_input_summary(
        "write",
        {"text": "hello brave new world"},
        display,
        rules,
    ) == "`hello brave ...`"
    assert format_tool_input_summary(
        "browse",
        {"url": "https://example.com/very/long/path"},
        display,
        rules,
    ) == "`https://exampl...`"


def test_format_tool_input_summary_handles_count_lsp_and_first_question_rules() -> None:
    """Specialized rule types should summarize counts, LSP ops, and questions."""
    rules = {
        "batch": {"type": "count", "keys": ["items"], "suffix": " files"},
        "lsp": {"type": "lsp", "op_key": "op", "path_keys": ["file"]},
        "ask": {"type": "first_question", "keys": ["questions"], "question_key": "question"},
    }
    display = _display()

    assert format_tool_input_summary(
        "batch",
        {"items": [1, 2, 3]},
        display,
        rules,
    ) == "`3 files`"
    assert format_tool_input_summary(
        "lsp",
        {"op": "rename", "file": "/tmp/project/src/module.py"},
        display,
        rules,
    ) == "`rename` on `...src/module.py`"
    assert format_tool_input_summary(
        "ask",
        {"questions": [{"question": "Proceed with deploy to production?"}]},
        display,
        rules,
    ) == "`Proceed with...`"


def test_format_tool_input_summary_returns_empty_for_missing_or_unusable_rules() -> None:
    """Unknown rules and empty question payloads should produce no summary."""
    rules = {
        "ask": {"type": "first_question", "keys": ["questions"]},
        "unknown": {"type": "mystery", "keys": ["value"]},
    }
    display = _display()

    assert format_tool_input_summary("missing", {}, display, rules) == ""
    assert format_tool_input_summary("ask", {"questions": []}, display, rules) == ""
    assert format_tool_input_summary("unknown", {"value": "x"}, display, rules) == ""


def test_tool_input_summary_helper_functions_cover_defaults() -> None:
    """Helper utilities should handle missing keys and short values directly."""
    assert truncate_path("/tmp/file.txt", max_len=32) == "/tmp/file.txt"
    assert truncate_cmd("echo hi", max_len=20) == "echo hi"
    assert _first_present({"b": 2}, ["a", "b"], default="fallback") == 2
    assert _first_present({}, ["a"], default="fallback") == "fallback"
