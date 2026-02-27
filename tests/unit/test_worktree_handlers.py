"""Unit tests for git worktree command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import Session
from src.git.models import Worktree
from src.handlers.claude.worktree import (
    _handle_add,
    _handle_list,
    _handle_merge,
    _handle_switch,
    register_worktree_commands,
)


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _ctx(channel_id: str = "C123", thread_ts: str = "123.456"):
    return SimpleNamespace(
        channel_id=channel_id,
        thread_ts=thread_ts,
        client=SimpleNamespace(chat_postMessage=AsyncMock()),
    )


def _deps_for_session(session: Session):
    return SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_cwd=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
        )
    )


@pytest.mark.asyncio
async def test_registers_worktree_and_alias_commands():
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace(get_or_create_session=AsyncMock()))

    register_worktree_commands(app, deps)

    assert "/worktree" in app.handlers
    assert "/wt" in app.handlers


@pytest.mark.asyncio
async def test_command_shows_usage_when_subcommand_missing():
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=True))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    ack = AsyncMock()
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=ack,
        command={"channel_id": "C123", "user_id": "U123", "text": "", "command": "/worktree"},
        client=client,
        logger=MagicMock(),
    )

    ack.assert_awaited_once()
    git_service.validate_git_repo.assert_awaited_once_with("/repo")
    assert client.chat_postMessage.await_args.kwargs["text"] == "Worktree usage"


@pytest.mark.asyncio
async def test_command_reports_not_git_repo():
    session = Session(working_directory="/not-a-repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=False))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "list",
            "command": "/worktree",
        },
        client=client,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == "Not a git repository"


@pytest.mark.asyncio
async def test_handle_add_updates_session_and_clears_ids():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id="codex-1")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(add_worktree=AsyncMock(return_value="/repo-worktrees/feature-x"))

    await _handle_add(ctx, deps, session, git_service, "feature-x")

    git_service.add_worktree.assert_awaited_once_with("/repo", "feature-x")
    deps.db.update_session_cwd.assert_awaited_once_with(
        "C123", "123.456", "/repo-worktrees/feature-x"
    )
    deps.db.clear_session_claude_id.assert_awaited_once_with("C123", "123.456")
    deps.db.clear_session_codex_id.assert_awaited_once_with("C123", "123.456")
    assert ctx.client.chat_postMessage.await_args.kwargs["text"] == "Created worktree: feature-x"


@pytest.mark.asyncio
async def test_handle_list_marks_only_containing_worktree_current():
    ctx = _ctx()
    session = Session(working_directory="/tmp/project-worktrees/feature2/subdir")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[
                Worktree(path="/tmp/project-worktrees/feature", branch="feature"),
                Worktree(path="/tmp/project-worktrees/feature2", branch="feature2"),
            ]
        )
    )

    await _handle_list(ctx, session, git_service)

    block_text = ctx.client.chat_postMessage.await_args.kwargs["blocks"][0]["text"]["text"]
    assert "`feature` - `/tmp/project-worktrees/feature`" in block_text
    assert "`feature2` - `/tmp/project-worktrees/feature2` :point_left: _current_" in block_text
    assert "`feature` - `/tmp/project-worktrees/feature` :point_left: _current_" not in block_text


@pytest.mark.asyncio
async def test_handle_switch_updates_session_for_matching_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id=None)
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[Worktree(path="/repo-worktrees/feature-x", branch="feature-x")]
        )
    )

    await _handle_switch(ctx, deps, session, git_service, "feature-x")

    deps.db.update_session_cwd.assert_awaited_once_with(
        "C123", "123.456", "/repo-worktrees/feature-x"
    )
    deps.db.clear_session_claude_id.assert_awaited_once_with("C123", "123.456")
    deps.db.clear_session_codex_id.assert_not_called()
    assert (
        ctx.client.chat_postMessage.await_args.kwargs["text"] == "Switched to worktree: feature-x"
    )


@pytest.mark.asyncio
async def test_handle_switch_reports_missing_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[Worktree(path="/repo", branch="main", is_main=True)])
    )

    await _handle_switch(ctx, deps, session, git_service, "feature-x")

    deps.db.update_session_cwd.assert_not_called()
    assert ctx.client.chat_postMessage.await_args.kwargs["text"] == "Worktree not found: feature-x"


@pytest.mark.asyncio
async def test_handle_merge_success_removes_source_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id="codex-1")
    deps = _deps_for_session(session)
    main_wt = Worktree(path="/repo", branch="main", is_main=True)
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[main_wt, source_wt]),
        merge_branch=AsyncMock(return_value=(True, "merged")),
        remove_worktree=AsyncMock(return_value=True),
    )

    await _handle_merge(ctx, deps, session, git_service, "feature-x")

    deps.db.update_session_cwd.assert_awaited_once_with("C123", "123.456", "/repo")
    deps.db.clear_session_claude_id.assert_awaited_once_with("C123", "123.456")
    deps.db.clear_session_codex_id.assert_awaited_once_with("C123", "123.456")
    git_service.merge_branch.assert_awaited_once_with("/repo", "feature-x")
    git_service.remove_worktree.assert_awaited_once_with("/repo", "/repo-worktrees/feature-x")
    block_text = ctx.client.chat_postMessage.await_args.kwargs["blocks"][0]["text"]["text"]
    assert "Worktree removed after successful merge." in block_text


@pytest.mark.asyncio
async def test_handle_merge_conflicts_do_not_remove_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    main_wt = Worktree(path="/repo", branch="main", is_main=True)
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[main_wt, source_wt]),
        merge_branch=AsyncMock(return_value=(False, "conflict details")),
        remove_worktree=AsyncMock(),
    )

    await _handle_merge(ctx, deps, session, git_service, "feature-x")

    git_service.remove_worktree.assert_not_called()
    assert ctx.client.chat_postMessage.await_args.kwargs["text"] == "Merge conflicts with feature-x"


@pytest.mark.asyncio
async def test_handle_merge_rejects_main_branch_self_merge():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    main_wt = Worktree(path="/repo", branch="main", is_main=True)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[main_wt]),
        merge_branch=AsyncMock(),
        remove_worktree=AsyncMock(),
    )

    await _handle_merge(ctx, deps, session, git_service, "main")

    deps.db.update_session_cwd.assert_not_called()
    deps.db.clear_session_claude_id.assert_not_called()
    git_service.merge_branch.assert_not_called()
    git_service.remove_worktree.assert_not_called()
    assert (
        ctx.client.chat_postMessage.await_args.kwargs["text"]
        == "Cannot merge branch into itself: main"
    )
