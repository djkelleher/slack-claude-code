"""Unit tests for workspace lease orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.database.migrations import init_database
from src.database.models import Session
from src.database.repository import DatabaseRepository
from src.git.models import Worktree
from src.git.workspace_manager import WorkspaceManager


@pytest_asyncio.fixture
async def db_repo(tmp_path):
    """Create a test database repository."""
    db_path = str(tmp_path / "workspace-manager.db")
    await init_database(db_path)
    return DatabaseRepository(db_path)


@pytest.mark.asyncio
async def test_prepare_workspace_uses_direct_lease_for_non_git_directory(db_repo, tmp_path):
    """Non-git directories should be leased directly and keep session persistence."""
    workdir = tmp_path / "plain-dir"
    workdir.mkdir()
    session = await db_repo.get_or_create_session("C123", None, str(workdir))
    session.model = "opus"

    manager = WorkspaceManager(
        db=db_repo,
        claude_executor=SimpleNamespace(has_active_execution=AsyncMock(return_value=False)),
        codex_executor=SimpleNamespace(has_active_execution=AsyncMock(return_value=False)),
        git_service=SimpleNamespace(validate_git_repo=AsyncMock(return_value=False)),
    )

    prepared = await manager.prepare_workspace(
        session=session,
        channel_id="C123",
        thread_ts=None,
        session_scope="C123",
        execution_id="exec-1",
    )

    assert prepared.lease.lease_kind == "direct"
    assert prepared.persist_session_ids is True
    assert prepared.session.working_directory == str(workdir.resolve())
    active = await db_repo.get_active_workspace_lease_by_root(str(workdir.resolve()))
    assert active is not None
    assert active.execution_id == "exec-1"


@pytest.mark.asyncio
async def test_prepare_workspace_uses_auto_worktree_when_current_root_is_leased(db_repo, tmp_path):
    """Concurrent git executions should move the later run into an auto worktree."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    auto_root = tmp_path / "repo-worktrees" / "slack-auto" / "c123-exec-2"
    session_one = await db_repo.get_or_create_session("C123", None, str(repo_root))
    session_one.model = "gpt-5.3-codex"
    session_two = Session(
        id=session_one.id,
        channel_id="C123",
        thread_ts=None,
        working_directory=str(repo_root),
        model="gpt-5.3-codex",
    )

    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        get_main_worktree=AsyncMock(return_value=str(repo_root)),
        list_worktrees=AsyncMock(
            return_value=[Worktree(path=str(repo_root), branch="main", is_main=True)]
        ),
        add_worktree=AsyncMock(return_value=str(auto_root)),
    )
    executor = SimpleNamespace(has_active_execution=AsyncMock(return_value=True))
    manager = WorkspaceManager(
        db=db_repo,
        claude_executor=executor,
        codex_executor=executor,
        git_service=git_service,
    )

    first = await manager.prepare_workspace(
        session=session_one,
        channel_id="C123",
        thread_ts=None,
        session_scope="C123",
        execution_id="exec-1",
    )
    second = await manager.prepare_workspace(
        session=session_two,
        channel_id="C123",
        thread_ts=None,
        session_scope="C123",
        execution_id="exec-2",
    )

    assert first.lease.lease_kind == "direct"
    assert second.lease.lease_kind == "worktree"
    assert second.persist_session_ids is False
    assert second.session.working_directory == str(auto_root.resolve())
    assert second.lease.target_worktree_path == str(repo_root.resolve())
    git_service.add_worktree.assert_awaited_once()
