"""Unit tests for Codex app-server approval bridge helpers."""

from src.codex.approval_bridge import (
    approval_payload_from_decision,
    default_approval_payload,
    format_approval_request_for_slack,
)


def test_format_command_approval_for_slack():
    """Command approvals map to run_command with concise input details."""
    tool_name, tool_input = format_approval_request_for_slack(
        "item/commandExecution/requestApproval",
        {
            "command": "git status",
            "cwd": "/tmp/workspace",
            "reason": "Needs repo state",
        },
    )

    assert tool_name == "run_command"
    assert "command: git status" in (tool_input or "")
    assert "cwd: /tmp/workspace" in (tool_input or "")
    assert "reason: Needs repo state" in (tool_input or "")


def test_approval_payload_decisions():
    """Decision mapping follows app-server method-specific enums."""
    assert approval_payload_from_decision("item/fileChange/requestApproval", True) == {
        "decision": "accept"
    }
    assert approval_payload_from_decision("item/fileChange/requestApproval", False) == {
        "decision": "decline"
    }
    assert approval_payload_from_decision("skill/requestApproval", True) == {
        "decision": "approve"
    }
    assert approval_payload_from_decision("skill/requestApproval", False) == {
        "decision": "decline"
    }


def test_default_approval_payload_uses_mode():
    """Default payload auto-accepts in never mode and declines otherwise."""
    assert default_approval_payload(
        "item/commandExecution/requestApproval", "never"
    ) == {"decision": "accept"}
    assert default_approval_payload(
        "item/commandExecution/requestApproval", "on-request"
    ) == {"decision": "decline"}
