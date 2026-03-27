"""Unit tests for worktree interactive action handlers."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import RollbackEvent, Session
from src.git.models import Worktree
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


def _base_body() -> dict:
    return {
        "channel": {"id": "C123"},
        "user": {"id": "U123"},
        "message": {"thread_ts": "123.456", "ts": "123.456"},
    }


@pytest.mark.asyncio
async def test_registers_worktree_action_handlers():
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace())

    register_actions(app, deps)

    assert "git_init_repo" in app.actions
    assert "worktree_switch" in app.actions
    assert "worktree_merge_current" in app.actions
    assert "worktree_remove" in app.actions


@pytest.mark.asyncio
async def test_git_init_action_initializes_repository_and_updates_message():
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace())
    register_actions(app, deps)

    git_service = SimpleNamespace(initialize_repo=AsyncMock(return_value="main"))
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        chat_postEphemeral=AsyncMock(),
    )

    with patch("src.handlers.actions.GitService", return_value=git_service):
        await app.actions["git_init_repo"](
            ack=AsyncMock(),
            action={"value": json.dumps({"cwd": "/repo"})},
            body=_base_body(),
            client=client,
            logger=MagicMock(),
        )

    git_service.initialize_repo.assert_awaited_once_with("/repo")
    client.chat_update.assert_awaited_once()
    kwargs = client.chat_update.await_args.kwargs
    assert kwargs["text"] == "Git repository created"
    assert "Git repository created" in kwargs["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_worktree_switch_action_updates_session_cwd():
    app = _FakeApp()
    session = Session(working_directory="/repo", codex_session_id="codex-1")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_cwd=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
        )
    )
    register_actions(app, deps)

    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[Worktree(path="/repo-worktrees/feature-x", branch="feature-x")]
        )
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock())

    with patch("src.handlers.actions.GitService", return_value=git_service):
        await app.actions["worktree_switch"](
            ack=AsyncMock(),
            action={
                "value": json.dumps({"branch": "feature-x", "path": "/repo-worktrees/feature-x"})
            },
            body=_base_body(),
            client=client,
            logger=MagicMock(),
        )

    deps.db.update_session_cwd.assert_awaited_once_with(
        "C123", "123.456", "/repo-worktrees/feature-x"
    )


@pytest.mark.asyncio
async def test_rollback_apply_rejects_preview_from_different_scope():
    app = _FakeApp()
    rollback_event = RollbackEvent(
        id=7,
        channel_id="C999",
        thread_ts="999.000",
        working_directory="/repo",
        target_commit="abc123",
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(get_rollback_event=AsyncMock(return_value=rollback_event)),
        trace_service=SimpleNamespace(),
    )
    register_actions(app, deps)

    client = SimpleNamespace(chat_postEphemeral=AsyncMock(), chat_postMessage=AsyncMock())
    await app.actions["rollback_apply"](
        ack=AsyncMock(),
        action={"value": "7"},
        body=_base_body(),
        client=client,
        logger=MagicMock(),
    )

    client.chat_postEphemeral.assert_awaited_once()
    assert (
        "does not belong to this channel/thread scope"
        in client.chat_postEphemeral.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_rollback_apply_rejects_when_session_cwd_changed_since_preview():
    app = _FakeApp()
    rollback_event = RollbackEvent(
        id=8,
        channel_id="C123",
        thread_ts="123.456",
        working_directory="/repo-a",
        target_commit="abc123",
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_rollback_event=AsyncMock(return_value=rollback_event),
            get_or_create_session=AsyncMock(
                return_value=Session(id=1, working_directory="/repo-b")
            ),
        ),
        executor=None,
        codex_executor=None,
        trace_service=SimpleNamespace(),
    )
    register_actions(app, deps)

    with patch("src.handlers.actions.TaskManager.get", new=AsyncMock(return_value=None)):
        client = SimpleNamespace(chat_postEphemeral=AsyncMock(), chat_postMessage=AsyncMock())
        await app.actions["rollback_apply"](
            ack=AsyncMock(),
            action={"value": "8"},
            body=_base_body(),
            client=client,
            logger=MagicMock(),
        )

    client.chat_postMessage.assert_awaited_once()
    assert (
        "working directory changed after preview"
        in client.chat_postMessage.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_worktree_merge_current_action_merges_into_current_worktree():
    app = _FakeApp()
    session = Session(working_directory="/repo-worktrees/target")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_cwd=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
        )
    )
    register_actions(app, deps)

    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[
                Worktree(path="/repo-worktrees/target", branch="target"),
                Worktree(path="/repo-worktrees/feature-x", branch="feature-x"),
            ]
        ),
        get_status=AsyncMock(
            side_effect=[
                SimpleNamespace(is_clean=True),
                SimpleNamespace(is_clean=True),
            ]
        ),
        merge_branch=AsyncMock(return_value=(True, "merged")),
        remove_worktree=AsyncMock(return_value=True),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock())

    with patch("src.handlers.actions.GitService", return_value=git_service):
        await app.actions["worktree_merge_current"](
            ack=AsyncMock(),
            action={
                "value": json.dumps({"branch": "feature-x", "path": "/repo-worktrees/feature-x"})
            },
            body=_base_body(),
            client=client,
            logger=MagicMock(),
        )

    git_service.merge_branch.assert_awaited_once_with("/repo-worktrees/target", "feature-x")


@pytest.mark.asyncio
async def test_worktree_remove_action_returns_ephemeral_error_when_blocked():
    app = _FakeApp()
    session = Session(working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_cwd=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
        )
    )
    register_actions(app, deps)

    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[Worktree(path="/repo", branch="main", is_main=True)]
        ),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=True)),
        remove_worktree=AsyncMock(),
        delete_branch=AsyncMock(),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock())

    with patch("src.handlers.actions.GitService", return_value=git_service):
        await app.actions["worktree_remove"](
            ack=AsyncMock(),
            action={"value": json.dumps({"branch": "main", "path": "/repo"})},
            body=_base_body(),
            client=client,
            logger=MagicMock(),
        )

    client.chat_postEphemeral.assert_awaited_once()
