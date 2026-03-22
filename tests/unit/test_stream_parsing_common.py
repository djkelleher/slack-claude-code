"""Unit tests for shared stream parsing helpers."""

from src.backends import stream_parsing_common as common
from src.utils.stream_models import BaseToolActivity


class _ToolActivity(BaseToolActivity):
    SUMMARY_RULES = {
        "run": {"type": "cmd", "keys": ["command"]},
    }


def test_parse_json_line_with_buffer_handles_partial_complete_and_overflow() -> None:
    """Buffered parsing should combine partial JSON and report overflow cleanly."""
    data, buffer, error = common.parse_json_line_with_buffer(
        line='{"a":',
        buffer="",
        max_buffer_size=32,
    )
    assert data is None
    assert buffer == '{"a":'
    assert error is None

    data, buffer, error = common.parse_json_line_with_buffer(
        line="1}",
        buffer=buffer,
        max_buffer_size=32,
    )
    assert data == {"a": 1}
    assert buffer == ""
    assert error is None

    data, buffer, error = common.parse_json_line_with_buffer(
        line=' {"b": 2} ',
        buffer="stale",
        max_buffer_size=32,
    )
    assert data == {"b": 2}
    assert buffer == "stale"
    assert error is None

    data, buffer, error = common.parse_json_line_with_buffer(
        line="   ",
        buffer="carry",
        max_buffer_size=32,
    )
    assert data is None
    assert buffer == "carry"
    assert error is None

    data, buffer, error = common.parse_json_line_with_buffer(
        line="x" * 40,
        buffer="",
        max_buffer_size=16,
    )
    assert data is None
    assert buffer == ""
    assert "overflow" in (error or "").lower()


def test_normalize_tool_input_supports_strings_dicts_none_and_raw_values() -> None:
    """Tool input normalization should accept multiple payload shapes."""
    assert common.normalize_tool_input('{"a": 1}') == {"a": 1}
    assert common.normalize_tool_input("not-json") == {"raw": "not-json"}
    assert common.normalize_tool_input({"a": 1}) == {"a": 1}
    assert common.normalize_tool_input(None) == {}
    assert common.normalize_tool_input(7) == {"raw": 7}


def test_create_tool_activity_tracks_collision_and_truncates_preview() -> None:
    """Tool creation should register the tool and trim long string previews."""
    pending_tools = {}

    first, first_details, first_collision = common.create_tool_activity(
        tool_cls=_ToolActivity,
        pending_tools=pending_tools,
        tool_id="tool-1",
        tool_name="run",
        tool_input={"command": "echo hi", "payload": "x" * 120},
    )
    second, _, second_collision = common.create_tool_activity(
        tool_cls=_ToolActivity,
        pending_tools=pending_tools,
        tool_id="tool-1",
        tool_name="run",
        tool_input={"command": "echo again"},
    )

    assert first.input_summary == "`echo hi`"
    assert "payload: " + ("x" * 100) + "..." in first_details
    assert first_collision is False
    assert second_collision is True
    assert pending_tools["tool-1"] is second


def test_create_tool_result_handles_pending_and_unknown_tools(monkeypatch) -> None:
    """Tool result creation should finalize known tools and synthesize unknown ones."""
    pending_tools = {}
    activity, _, _ = common.create_tool_activity(
        tool_cls=_ToolActivity,
        pending_tools=pending_tools,
        tool_id="tool-1",
        tool_name="run",
        tool_input={"command": "echo hi"},
    )
    activity.started_at = 10.0
    monkeypatch.setattr(common.time, "monotonic", lambda: 10.25)

    activities, details = common.create_tool_result(
        tool_cls=_ToolActivity,
        pending_tools=pending_tools,
        tool_use_id="tool-1",
        content="ok" * 400,
        is_error=False,
    )
    unknown_activities, unknown_details = common.create_tool_result(
        tool_cls=_ToolActivity,
        pending_tools=pending_tools,
        tool_use_id="missing",
        content="boom",
        is_error=True,
    )

    assert activities[0].id == "tool-1"
    assert activities[0].duration_ms == 250
    assert activities[0].full_result == "ok" * 400
    assert activities[0].result.endswith("...")
    assert "[Tool Result: SUCCESS]" in details

    assert unknown_activities[0].name == "unknown"
    assert unknown_activities[0].is_error is True
    assert "[Tool Result: ERROR]" in unknown_details
