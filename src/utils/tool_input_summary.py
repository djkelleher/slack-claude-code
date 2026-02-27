"""Shared tool input summary helpers for streaming parsers."""

from typing import Any, Mapping, Sequence


def truncate_path(path: str, max_len: int = 45) -> str:
    """Truncate file path, keeping filename visible."""
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3) :]


def truncate_cmd(cmd: str, max_len: int = 50) -> str:
    """Truncate command for display."""
    command_text = cmd.replace("\n", " ").strip()
    if len(command_text) <= max_len:
        return command_text
    return command_text[: max_len - 3] + "..."


def _first_present(input_dict: Mapping[str, Any], keys: Sequence[str], default: Any = "?") -> Any:
    """Return first present key from input_dict."""
    for key in keys:
        if key in input_dict:
            return input_dict[key]
    return default


def _truncate_text(value: Any, max_len: int) -> str:
    """Truncate a value converted to string."""
    text = str(value)
    return f"{text[:max_len]}{'...' if len(text) > max_len else ''}"


def format_tool_input_summary(
    name: str,
    input_dict: Mapping[str, Any],
    display: Any,
    rules: Mapping[str, Mapping[str, Any]],
) -> str:
    """Create a short summary string based on a rule table."""
    rule = rules.get(name)
    if not rule:
        return ""

    rule_type = rule["type"]
    keys = rule.get("keys", [])

    if rule_type == "path":
        value = _first_present(input_dict, keys, "?")
        return f"`{truncate_path(str(value), display.truncate_path_length)}`"

    if rule_type == "cmd":
        value = _first_present(input_dict, keys, "?")
        return f"`{truncate_cmd(str(value), display.truncate_cmd_length)}`"

    if rule_type == "pattern":
        value = _first_present(input_dict, keys, "?")
        return f"`{_truncate_text(value, display.truncate_pattern_length)}`"

    if rule_type == "text":
        value = _first_present(input_dict, keys, "?")
        return f"`{_truncate_text(value, display.truncate_text_length)}`"

    if rule_type == "url":
        value = _first_present(input_dict, keys, "?")
        return f"`{_truncate_text(value, display.truncate_url_length)}`"

    if rule_type == "count":
        value = _first_present(input_dict, keys, [])
        count = (
            len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else 0
        )
        suffix = str(rule.get("suffix", " items"))
        return f"`{count}{suffix}`"

    if rule_type == "lsp":
        op_key = str(rule.get("op_key", "operation"))
        path_keys = rule.get("path_keys", ["filePath"])
        operation = input_dict.get(op_key, "?")
        path = _first_present(input_dict, path_keys, "?")
        return f"`{operation}` on `{truncate_path(str(path), display.truncate_path_length)}`"

    if rule_type == "first_question":
        questions = _first_present(input_dict, keys, [])
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict):
                question_key = str(rule.get("question_key", "question"))
                question = first.get(question_key, "")
                if question:
                    return f"`{_truncate_text(question, display.truncate_text_length)}`"
        return ""

    return ""
