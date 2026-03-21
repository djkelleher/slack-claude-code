"""Shared stream parsing helpers for Claude and Codex backends."""

import json
import time
from typing import Any

from src.utils.stream_models import BaseToolActivity


def _preview_tool_value(value: Any, max_len: int = 100) -> Any:
    """Truncate long string tool inputs for detailed activity output."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    return value


def parse_json_line_with_buffer(
    *,
    line: str,
    buffer: str,
    max_buffer_size: int,
) -> tuple[Any | None, str, str | None]:
    """Parse a line as JSON, buffering partial chunks safely."""
    normalized = line.strip()
    if not normalized:
        return None, buffer, None

    try:
        data = json.loads(normalized)
        return data, buffer, None
    except json.JSONDecodeError:
        buffer += normalized
        if len(buffer) > max_buffer_size:
            return (
                None,
                "",
                (
                    f"Stream buffer overflow ({len(buffer)} bytes exceeds "
                    f"{max_buffer_size} limit)."
                ),
            )
        try:
            data = json.loads(buffer)
            return data, "", None
        except json.JSONDecodeError:
            return None, buffer, None


def normalize_tool_input(tool_input: Any) -> dict:
    """Normalize tool input payload into a dictionary."""
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return {"raw": tool_input}
    if isinstance(tool_input, dict):
        return tool_input
    if tool_input is None:
        return {}
    return {"raw": tool_input}


def create_tool_activity(
    *,
    tool_cls: type[BaseToolActivity],
    pending_tools: dict[str, BaseToolActivity],
    tool_id: str,
    tool_name: str,
    tool_input: Any,
) -> tuple[BaseToolActivity, str, bool]:
    """Create and register a tool activity with standardized detailed output."""
    normalized_input = normalize_tool_input(tool_input)
    tool_activity = tool_cls(
        id=tool_id,
        name=tool_name,
        input=normalized_input,
        input_summary=tool_cls.create_input_summary(tool_name, normalized_input),
        started_at=time.monotonic(),
        timestamp=time.time(),
    )
    collision = tool_id in pending_tools
    pending_tools[tool_id] = tool_activity

    detailed_addition = f"\n\n[Tool: {tool_name}]\n"
    for key, value in normalized_input.items():
        detailed_addition += f"  {key}: {_preview_tool_value(value)}\n"

    return tool_activity, detailed_addition, collision


def create_tool_result(
    *,
    tool_cls: type[BaseToolActivity],
    pending_tools: dict[str, BaseToolActivity],
    tool_use_id: str,
    content: str,
    is_error: bool,
) -> tuple[list[BaseToolActivity], str]:
    """Create standardized tool-result activities and detailed output text."""
    full_content = content or ""
    content_preview = full_content[:500] + "..." if len(full_content) > 500 else full_content
    tool_activities: list[BaseToolActivity] = []

    if tool_use_id in pending_tools:
        tool_activity = pending_tools.pop(tool_use_id)
        tool_activity.result = content_preview
        tool_activity.full_result = full_content
        tool_activity.is_error = is_error
        if tool_activity.started_at:
            tool_activity.duration_ms = int((time.monotonic() - tool_activity.started_at) * 1000)
        tool_activities.append(tool_activity)
    else:
        tool_activities.append(
            tool_cls(
                id=tool_use_id,
                name="unknown",
                input={},
                input_summary="",
                result=content_preview,
                full_result=full_content,
                is_error=is_error,
            )
        )

    status = "ERROR" if is_error else "SUCCESS"
    detailed_addition = f"\n\n[Tool Result: {status}]\n{content_preview}\n"
    return tool_activities, detailed_addition
