"""Unit tests for stream accumulator git tool tracking."""

from src.backends.stream_accumulator import StreamAccumulator
from src.utils.stream_models import BaseToolActivity, StreamMessage


def test_stream_accumulator_collects_git_shell_tool_results() -> None:
    """Completed git shell commands should be captured as raw git events."""
    accumulator = StreamAccumulator(join_assistant_chunks=lambda left, right: left + right)
    tool = BaseToolActivity(
        id="tool-1",
        name="run_command",
        input={"command": '/bin/bash -lc "git commit -m test"'},
        input_summary="",
        result="[main abc123] test",
        full_result="[main abc123] test",
        is_error=False,
        duration_ms=120,
    )

    accumulator.apply(
        StreamMessage(
            type="tool_result",
            tool_activities=[tool],
            raw={},
        )
    )

    assert accumulator.git_tool_events == [
        {
            "kind": "shell",
            "tool_id": "tool-1",
            "tool_name": "run_command",
            "command": '/bin/bash -lc "git commit -m test"',
            "result": "[main abc123] test",
            "is_error": False,
            "duration_ms": 120,
        }
    ]


def test_stream_accumulator_collects_git_mcp_tool_results() -> None:
    """Completed git MCP calls should be captured as raw git events."""
    accumulator = StreamAccumulator(join_assistant_chunks=lambda left, right: left + right)
    tool = BaseToolActivity(
        id="tool-2",
        name="mcp_tool_call",
        input={"server": "git", "tool": "status"},
        input_summary="",
        result="clean",
        full_result="clean",
        is_error=False,
        duration_ms=40,
    )

    accumulator.apply(
        StreamMessage(
            type="tool_result",
            tool_activities=[tool],
            raw={},
        )
    )

    assert accumulator.git_tool_events == [
        {
            "kind": "mcp",
            "tool_id": "tool-2",
            "tool_name": "mcp_tool_call",
            "server": "git",
            "mcp_tool": "status",
            "result": "clean",
            "is_error": False,
            "duration_ms": 40,
        }
    ]


def test_stream_accumulator_ignores_non_git_tool_results() -> None:
    """Non-git tool results should not be recorded as git events."""
    accumulator = StreamAccumulator(join_assistant_chunks=lambda left, right: left + right)
    tool = BaseToolActivity(
        id="tool-3",
        name="run_command",
        input={"command": "ls -la"},
        input_summary="",
        result="README.md",
        full_result="README.md",
        is_error=False,
        duration_ms=5,
    )

    accumulator.apply(
        StreamMessage(
            type="tool_result",
            tool_activities=[tool],
            raw={},
        )
    )

    assert accumulator.git_tool_events == []
