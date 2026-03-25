"""Unit tests for top-level command registration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_bolt.async_app import AsyncApp

from src.handlers import register_commands
from src.handlers.actions import register_actions


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}
        self.actions: dict[str, object] = {}
        self.views: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator

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


def test_register_commands_excludes_codex_slash_commands():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    register_commands(app, db, claude_executor, codex_executor=codex_executor)

    assert "/usage" in app.handlers
    assert "/clear" in app.handlers
    assert "/!" in app.handlers
    assert "/git" not in app.handlers
    assert "/claude-help" not in app.handlers
    assert "/doctor" not in app.handlers
    assert "/claude-config" not in app.handlers
    assert "/memory" not in app.handlers
    assert "/stats" not in app.handlers
    assert "/todos" not in app.handlers
    assert "/codex-status" not in app.handlers
    assert "/codex-clear" not in app.handlers
    assert "/codex-sessions" not in app.handlers
    assert "/codex-cleanup" not in app.handlers
    assert "/codex-thread" not in app.handlers
    assert "/codex-config" not in app.handlers
    assert "/codex-metrics" not in app.handlers


def test_register_commands_builds_slash_command_router():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    deps = register_commands(app, db, claude_executor, codex_executor=codex_executor)

    assert deps.slash_command_router is not None
    assert deps.slash_command_router.has_command("/clear")


def test_register_commands_builds_slash_router_for_real_async_app():
    app = AsyncApp(
        token="xoxb-test", signing_secret="test-signing-secret", process_before_response=True
    )
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    deps = register_commands(app, db, claude_executor, codex_executor=codex_executor)

    assert deps.slash_command_router.has_command("/clear")
    assert deps.slash_command_router.has_command("/q")


def test_register_actions_includes_worktree_buttons():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    deps = register_commands(app, db, claude_executor, codex_executor=codex_executor)
    register_actions(app, deps)

    assert "worktree_switch" in app.actions
    assert "worktree_merge_current" in app.actions
    assert "worktree_remove" in app.actions


@pytest.mark.asyncio
async def test_custom_model_action_modal_mentions_space_separated_effort_input():
    app = _FakeApp()
    db = SimpleNamespace()
    claude_executor = SimpleNamespace()
    codex_executor = SimpleNamespace()

    deps = register_commands(app, db, claude_executor, codex_executor=codex_executor)
    register_actions(app, deps)

    handler = app.actions["select_model_custom"]
    client = SimpleNamespace(views_open=AsyncMock())

    await handler(
        ack=AsyncMock(),
        action={"value": "C123|123.456"},
        body={"trigger_id": "trigger-123"},
        client=client,
        logger=MagicMock(),
    )

    client.views_open.assert_awaited_once()
    hint_text = client.views_open.await_args.kwargs["view"]["blocks"][0]["hint"]["text"]
    assert "<model> [effort]" in hint_text
    assert "gpt-5.4 high" in hint_text
    assert "claude-opus-4-6 auto" in hint_text
