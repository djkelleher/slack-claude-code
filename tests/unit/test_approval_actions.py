"""Unit tests for approval action handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers.actions import register_actions


class _FakeApp:
    """Minimal Slack app stub for action registration tests."""

    def __init__(self):
        self.actions: dict[str, object] = {}
        self.views: dict[str, object] = {}

    def action(self, name):
        def decorator(func):
            self.actions[str(name)] = func
            return func

        return decorator

    def view(self, name):
        def decorator(func):
            self.views[str(name)] = func
            return func

        return decorator


def _approval_body() -> dict:
    return {
        "channel": {"id": "C123"},
        "user": {"id": "U123"},
        "message": {"ts": "123.456", "thread_ts": "123.456"},
    }


@pytest.mark.asyncio
async def test_approve_tool_action_passes_resolver_user_and_updates_blocks() -> None:
    app = _FakeApp()
    register_actions(app, SimpleNamespace(db=SimpleNamespace()))

    client = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    resolved = SimpleNamespace(tool_name="exec_command")

    with patch(
        "src.handlers.actions.PermissionManager.resolve",
        new=AsyncMock(return_value=resolved),
    ) as mock_resolve:
        await app.actions["approve_tool"](
            ack=AsyncMock(),
            action={"value": "approval-123"},
            body=_approval_body(),
            client=client,
            logger=MagicMock(),
        )

    mock_resolve.assert_awaited_once_with("approval-123", approved=True, resolved_by="U123")
    client.chat_update.assert_awaited_once()
    blocks = client.chat_update.await_args.kwargs["blocks"]
    assert blocks[0]["text"]["text"] == ":heavy_check_mark: *Approved*: exec_command"
    assert blocks[1]["elements"][1]["text"] == "By: <@U123>"
