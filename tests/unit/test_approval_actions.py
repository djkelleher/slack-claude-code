"""Unit tests for approval action handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers.actions import register_actions
from src.utils.detail_cache import DetailCache


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


def _regex_model_action(app: _FakeApp):
    for key, handler in app.actions.items():
        if key.startswith("re.compile(") and "select_model_" in key:
            return handler
    raise AssertionError("Regex model action handler not registered")


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
    assert client.chat_update.await_args.kwargs["text"] == "Tool approval resolved"
    blocks = client.chat_update.await_args.kwargs["blocks"]
    assert blocks[0]["text"]["text"] == ":heavy_check_mark: *Approved*: exec_command"
    assert blocks[1]["elements"][1]["text"] == "By: <@U123>"


@pytest.mark.asyncio
async def test_view_detailed_output_falls_back_to_database_when_cache_is_empty() -> None:
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(get_command_detailed_output=AsyncMock(return_value="persisted details"))
    )
    register_actions(app, deps)
    DetailCache.clear()

    client = SimpleNamespace(views_open=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = _approval_body()
    body["trigger_id"] = "trigger-123"

    await app.actions["view_detailed_output"](
        ack=AsyncMock(),
        action={"value": "42"},
        body=body,
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_command_detailed_output.assert_awaited_once_with(42)
    client.views_open.assert_awaited_once()
    view_blocks = client.views_open.await_args.kwargs["view"]["blocks"]
    assert "persisted details" in view_blocks[0]["text"]["text"]


@pytest.mark.asyncio
async def test_model_menu_selection_handler_accepts_missing_value_payload() -> None:
    app = _FakeApp()
    register_actions(
        app,
        SimpleNamespace(
            db=SimpleNamespace(
                get_or_create_session=AsyncMock(return_value=SimpleNamespace(model="gpt-5.4")),
            )
        ),
    )

    client = SimpleNamespace(chat_postMessage=AsyncMock())
    body = _approval_body()
    action = {
        "action_id": "select_model_menu",
        "selected_option": {"value": "gpt-5.4"},
    }

    with patch(
        "src.handlers.actions._set_session_model_and_notify",
        new=AsyncMock(),
    ) as mock_set_model:
        await app.actions["select_model_menu"](
            ack=AsyncMock(),
            action=action,
            body=body,
            client=client,
            logger=MagicMock(),
        )

    mock_set_model.assert_awaited_once()
    assert mock_set_model.await_args.kwargs["channel_id"] == "C123"
    assert mock_set_model.await_args.kwargs["thread_ts"] == "123.456"


@pytest.mark.asyncio
async def test_effort_menu_selection_handler_updates_model_effort() -> None:
    app = _FakeApp()
    register_actions(
        app,
        SimpleNamespace(
            db=SimpleNamespace(
                get_or_create_session=AsyncMock(return_value=SimpleNamespace(model="gpt-5.4")),
            )
        ),
    )

    client = SimpleNamespace(chat_postMessage=AsyncMock())
    body = _approval_body()
    action = {
        "action_id": "select_effort_menu",
        "selected_option": {"value": "high"},
    }

    with patch(
        "src.handlers.actions._set_session_model_and_notify",
        new=AsyncMock(),
    ) as mock_set_model:
        await app.actions["select_effort_menu"](
            ack=AsyncMock(),
            action=action,
            body=body,
            client=client,
            logger=MagicMock(),
        )

    mock_set_model.assert_awaited_once()
    assert mock_set_model.await_args.kwargs["model_value"] == "gpt-5.4-high"
