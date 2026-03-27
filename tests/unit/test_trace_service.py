"""Unit tests for trace service orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import RollbackEvent, TraceCommit
from src.git.models import GitStatus
from src.trace.service import TraceService


class TestTraceService:
    """Tests for trace reporting and rollback behavior."""

    @pytest.mark.asyncio
    async def test_preview_rollback_marks_already_at_target(self):
        """Rollback previews should expose idempotent already-at-target state."""
        rollback_event = RollbackEvent(
            id=9,
            channel_id="CTRACE",
            thread_ts="123.456",
            target_commit="abc123def456",
            preview_diff="",
        )
        db = SimpleNamespace(create_rollback_event=AsyncMock(return_value=rollback_event))
        git_service = SimpleNamespace(
            resolve_commit=AsyncMock(return_value="abc123def456"),
            get_head_commit_hash=AsyncMock(return_value="abc123def456"),
            get_diff_between=AsyncMock(return_value=""),
            get_preferred_remote=AsyncMock(return_value=("origin", "git@github.com:org/repo.git")),
            build_commit_url=MagicMock(
                return_value="https://github.com/org/repo/commit/abc123def456"
            ),
            build_compare_url=MagicMock(
                return_value="https://github.com/org/repo/compare/abc123def456...abc123def456"
            ),
        )
        service = TraceService(db, git_service=git_service)

        event, preview = await service.preview_rollback(
            channel_id="CTRACE",
            thread_ts="123.456",
            working_directory="/repo",
            target_commit="HEAD",
            trace_run_id=17,
        )

        assert event.id == 9
        assert preview.target_commit == "abc123def456"
        assert preview.current_head == "abc123def456"
        assert preview.already_at_target is True
        assert preview.commit_url == "https://github.com/org/repo/commit/abc123def456"
        assert (
            preview.compare_url == "https://github.com/org/repo/compare/abc123def456...abc123def456"
        )
        db.create_rollback_event.assert_awaited_once_with(
            trace_run_id=17,
            channel_id="CTRACE",
            thread_ts="123.456",
            target_commit="abc123def456",
            preview_diff="",
        )

    @pytest.mark.asyncio
    async def test_apply_rollback_resets_and_records_event_when_clean(self):
        """Applying a rollback should reset and persist event metadata."""
        preview_event = RollbackEvent(
            id=3,
            channel_id="CTRACE",
            thread_ts="123.456",
            target_commit="abc123def456",
            preview_diff="stat",
        )
        applied_event = RollbackEvent(
            id=3,
            channel_id="CTRACE",
            thread_ts="123.456",
            target_commit="abc123def456",
            preview_diff="stat",
            status="applied",
            applied=True,
        )
        db = SimpleNamespace(
            get_rollback_event=AsyncMock(side_effect=[preview_event, applied_event]),
            update_rollback_event=AsyncMock(return_value=True),
            create_trace_event=AsyncMock(),
            create_checkpoint=AsyncMock(),
        )
        git_service = SimpleNamespace(
            get_status=AsyncMock(return_value=GitStatus(branch="main", is_clean=True)),
            reset_hard=AsyncMock(),
            create_checkpoint=AsyncMock(),
        )
        service = TraceService(db, git_service=git_service)
        service._is_git_repo = AsyncMock(return_value=True)

        result = await service.apply_rollback(
            rollback_event_id=3,
            working_directory="/repo",
            session_id=11,
            channel_id="CTRACE",
            trace_run_id=21,
        )

        assert result.status == "applied"
        assert result.applied is True
        git_service.reset_hard.assert_awaited_once_with("/repo", "abc123def456")
        git_service.create_checkpoint.assert_not_awaited()
        db.create_checkpoint.assert_not_awaited()
        db.update_rollback_event.assert_awaited_once_with(
            3,
            status="applied",
            applied=True,
            checkpoint_name=None,
            checkpoint_ref=None,
        )
        db.create_trace_event.assert_awaited_once()

    def test_format_commit_snapshot_renders_lineage_metadata(self):
        """Commit snapshot formatting should preserve parent and origin metadata."""
        summary, content = TraceService.format_commit_snapshot(
            [
                TraceCommit(
                    trace_run_id=1,
                    commit_hash="def456",
                    parent_hash="abc123",
                    short_hash="def456",
                    subject="Add tracing",
                    author_name="Codex",
                    authored_at="2026-03-27T12:00:00+00:00",
                    origin="system",
                    diff="diff --git a/app.py b/app.py",
                )
            ]
        )

        assert summary == "1 commit(s): def456 Add tracing"
        assert "parent_hash: abc123" in content
        assert "origin: system" in content
        assert "diff --git a/app.py b/app.py" in content
