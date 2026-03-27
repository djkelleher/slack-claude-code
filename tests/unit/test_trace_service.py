"""Unit tests for trace service orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import RollbackEvent, TraceCommit, TraceConfig, TraceMilestone, TraceRun
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

    @pytest.mark.asyncio
    async def test_start_explicit_milestone_closes_previous_active_milestone(self):
        """Explicit milestones should replace the current active explicit milestone."""
        current = TraceMilestone(
            id=4, session_id=1, channel_id="CTRACE", name="Old", mode="explicit"
        )
        replacement = TraceMilestone(
            id=5,
            session_id=1,
            channel_id="CTRACE",
            name="New",
            mode="explicit",
        )
        db = SimpleNamespace(
            get_active_explicit_trace_milestone=AsyncMock(return_value=current),
            complete_trace_milestone=AsyncMock(return_value=True),
            create_trace_milestone=AsyncMock(return_value=replacement),
        )
        service = TraceService(db, git_service=SimpleNamespace())

        milestone = await service.start_explicit_milestone(
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            name="New",
        )

        assert milestone.id == 5
        db.complete_trace_milestone.assert_awaited_once_with(
            4,
            summary="Closed by explicit milestone change",
        )
        db.create_trace_milestone.assert_awaited_once_with(
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            name="New",
            mode="explicit",
            root_key=None,
        )

    @pytest.mark.asyncio
    async def test_resolve_milestone_uses_active_explicit_milestone(self):
        """Active explicit milestones should attach subsequent traced runs."""
        config = TraceConfig(channel_id="CTRACE", thread_ts="123.456", report_milestone=True)
        explicit = TraceMilestone(
            id=7,
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            name="Release",
            mode="explicit",
        )
        db = SimpleNamespace(
            get_active_explicit_trace_milestone=AsyncMock(return_value=explicit),
        )
        service = TraceService(db, git_service=SimpleNamespace())

        milestone = await service._resolve_milestone(
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            prompt="Ship release",
            config_obj=config,
            queue_item_id=None,
            root_key=None,
            milestone_name=None,
        )

        assert milestone is explicit

    @pytest.mark.asyncio
    async def test_finalize_failed_run_skips_managed_commit_when_repo_started_dirty(self):
        """Managed fallback commits should not absorb pre-existing dirty changes."""
        trace_run = TraceRun(
            id=12,
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            execution_id="exec-12",
            backend="codex",
            working_directory="/repo",
            prompt="Ship fix",
            status="running",
            git_base_commit="abc123",
            git_base_is_clean=False,
            remote_url="git@github.com:org/repo.git",
        )
        refreshed = TraceRun(
            **{
                **trace_run.__dict__,
                "status": "completed",
                "summary": "completed: no commits, no verification noted",
            }
        )
        db = SimpleNamespace(
            get_trace_run=AsyncMock(side_effect=[trace_run, refreshed]),
            get_trace_config=AsyncMock(
                return_value=TraceConfig(channel_id="CTRACE", thread_ts="123.456", enabled=True)
            ),
            replace_trace_commits=AsyncMock(return_value=[]),
            update_trace_run=AsyncMock(return_value=True),
            create_trace_event=AsyncMock(),
            complete_trace_milestone=AsyncMock(),
        )
        git_service = SimpleNamespace(
            get_head_commit_hash=AsyncMock(return_value="abc123"),
            get_status=AsyncMock(return_value=GitStatus(branch="main", modified=["app.py"])),
            get_commit_diffs_since=AsyncMock(return_value=[]),
            stage_all_changes=AsyncMock(),
        )
        service = TraceService(db, git_service=git_service)
        service._is_git_repo = AsyncMock(return_value=True)
        service._schedule_openlineage_export = MagicMock()

        finalized = await service.finalize_run_with_status(
            trace_run_id=12,
            final_status="completed",
            output="done",
            error=None,
            git_tool_events=[],
        )

        assert finalized.run.status == "completed"
        git_service.get_commit_diffs_since.assert_awaited_once_with("/repo", "abc123")
        git_service.stage_all_changes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_build_queue_summary_uses_only_processed_queue_items(self):
        """Queue summaries should aggregate only the queue items from the drained queue."""
        config = TraceConfig(
            channel_id="CTRACE",
            thread_ts="123.456",
            enabled=True,
            report_queue_end=True,
        )
        run_one = TraceRun(
            id=1, session_id=1, channel_id="CTRACE", thread_ts="123.456", queue_item_id=10
        )
        run_two = TraceRun(
            id=2,
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            queue_item_id=11,
            milestone_id=3,
        )
        summary_row = SimpleNamespace(id=99, summary_text="Queue trace summary")
        db = SimpleNamespace(
            get_trace_config=AsyncMock(return_value=config),
            get_trace_run_by_queue_item=AsyncMock(side_effect=[run_one, run_two]),
            list_trace_commits=AsyncMock(side_effect=[[TraceCommit(trace_run_id=1)], []]),
            get_trace_milestone=AsyncMock(
                return_value=TraceMilestone(id=3, session_id=1, channel_id="CTRACE", name="M1")
            ),
            create_trace_queue_summary=AsyncMock(return_value=summary_row),
        )
        service = TraceService(db, git_service=SimpleNamespace())

        summary = await service.build_queue_summary(
            session_id=1,
            channel_id="CTRACE",
            thread_ts="123.456",
            status_counts={"completed": 2, "failed": 0, "cancelled": 0},
            queue_item_ids=[10, 11, 10],
        )

        assert summary is summary_row
        assert db.get_trace_run_by_queue_item.await_count == 2
        db.create_trace_queue_summary.assert_awaited_once()
