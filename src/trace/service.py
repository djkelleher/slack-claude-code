"""Traceability, lineage, rollback, and reporting orchestration."""

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import aiohttp
from loguru import logger

from src.config import config
from src.database.models import (
    TraceEvent,
    RollbackEvent,
    TraceCommit,
    TraceConfig,
    TraceMilestone,
    TraceQueueSummary,
    TraceRun,
)
from src.git.service import GitError, GitService


@dataclass(frozen=True)
class StartedTraceRun:
    """Active trace run metadata returned when execution begins."""

    run: TraceRun
    config: TraceConfig


@dataclass(frozen=True)
class FinalizedTraceRun:
    """Finalized trace run payload returned after execution completes."""

    run: TraceRun
    commits: list[TraceCommit]
    tool_events: list[TraceEvent]
    queue_summary_text: Optional[str] = None
    milestone_report: Optional["MilestoneReport"] = None


@dataclass(frozen=True)
class MilestoneReport:
    """Aggregated milestone reporting payload."""

    milestone: TraceMilestone
    runs: list[TraceRun]
    commits: list[TraceCommit]
    summary_text: str


@dataclass(frozen=True)
class QueueTraceReport:
    """Queue-end reporting payload including milestone summaries."""

    queue_summary: Optional[TraceQueueSummary]
    milestone_reports: list[MilestoneReport]


@dataclass(frozen=True)
class RollbackPreview:
    """Computed rollback preview details."""

    target_commit: str
    current_head: Optional[str]
    preview_key: str
    diff_text: str
    commit_url: Optional[str]
    compare_url: Optional[str]
    already_at_target: bool


class TraceService:
    """Centralized traceability orchestration across prompt and queue execution."""

    def __init__(self, db: Any, git_service: Optional[GitService] = None) -> None:
        self.db = db
        self.git_service = git_service or GitService()

    async def get_config(self, channel_id: str, thread_ts: Optional[str]) -> TraceConfig:
        """Return trace configuration for one session scope."""
        return await self.db.get_trace_config(channel_id, thread_ts)

    async def enable_scope(
        self,
        channel_id: str,
        thread_ts: Optional[str],
        *,
        openlineage_enabled: Optional[bool] = None,
    ) -> TraceConfig:
        """Enable tracing for a scope."""
        return await self.db.upsert_trace_config(
            channel_id,
            thread_ts,
            enabled=True,
            auto_commit=True,
            report_step=True,
            report_milestone=True,
            report_queue_end=True,
            report_tool=False,
            openlineage_enabled=openlineage_enabled,
        )

    async def disable_scope(self, channel_id: str, thread_ts: Optional[str]) -> TraceConfig:
        """Disable tracing for a scope."""
        return await self.db.upsert_trace_config(channel_id, thread_ts, enabled=False)

    async def start_explicit_milestone(
        self,
        *,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        name: str,
    ) -> TraceMilestone:
        """Close any current explicit milestone and open a new one for the scope."""
        active_milestone = await self.db.get_active_explicit_trace_milestone(channel_id, thread_ts)
        if active_milestone is not None:
            await self.db.complete_trace_milestone(
                active_milestone.id,
                summary="Closed by explicit milestone change",
            )
        return await self.db.create_trace_milestone(
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            name=name,
            mode="explicit",
            root_key=None,
        )

    async def start_run(
        self,
        *,
        session: Any,
        channel_id: str,
        thread_ts: Optional[str],
        command_id: Optional[int],
        queue_item_id: Optional[int],
        execution_id: str,
        prompt: str,
        logical_run_id: Optional[str] = None,
        parent_run_id: Optional[int] = None,
        root_run_id: Optional[int] = None,
        root_key: Optional[str] = None,
        milestone_name: Optional[str] = None,
    ) -> Optional[StartedTraceRun]:
        """Create a trace run when tracing is enabled for the scope."""
        trace_config = await self.get_config(channel_id, thread_ts)
        if not trace_config.enabled:
            return None

        working_directory = session.working_directory
        backend = session.get_backend()
        model = session.model
        git_base_commit = None
        git_base_is_clean = None
        git_branch = None
        remote_name = None
        remote_url = None
        if await self._is_git_repo(working_directory):
            try:
                git_base_commit = await self.git_service.get_head_commit_hash(working_directory)
                git_base_status = await self.git_service.get_status(working_directory)
                git_base_is_clean = git_base_status.is_clean
                git_branch = await self.git_service.get_current_branch(working_directory)
                remote_name, remote_url = await self.git_service.get_preferred_remote(
                    working_directory
                )
            except GitError:
                git_base_commit = None
                git_base_is_clean = None
                git_branch = None
                remote_name = None
                remote_url = None

        milestone = await self._resolve_milestone(
            session_id=session.id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            prompt=prompt,
            config_obj=trace_config,
            queue_item_id=queue_item_id,
            root_key=root_key,
            milestone_name=milestone_name,
        )
        trace_run = await self.db.create_trace_run(
            session_id=session.id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            command_id=command_id,
            queue_item_id=queue_item_id,
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            milestone_id=milestone.id if milestone else None,
            logical_run_id=logical_run_id or execution_id,
            execution_id=execution_id,
            backend=backend,
            model=model,
            working_directory=working_directory,
            prompt=prompt,
            git_base_commit=git_base_commit,
            git_base_is_clean=git_base_is_clean,
            git_branch=git_branch,
            remote_name=remote_name,
            remote_url=remote_url,
        )
        await self.db.create_trace_event(
            trace_run_id=trace_run.id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            event_type="run_started",
            payload={
                "backend": backend,
                "model": model,
                "queue_item_id": queue_item_id,
                "command_id": command_id,
                "milestone_id": milestone.id if milestone else None,
                "logical_run_id": logical_run_id or execution_id,
                "root_key": root_key,
            },
        )
        self._schedule_openlineage_export(trace_config, trace_run, event_type="START")
        refreshed = await self.db.get_trace_run(trace_run.id)
        if refreshed is None:
            raise RuntimeError("Trace run disappeared immediately after creation")
        return StartedTraceRun(run=refreshed, config=trace_config)

    async def finalize_run(
        self,
        *,
        trace_run_id: int,
        success: bool,
        output: str,
        error: Optional[str],
        git_tool_events: list[dict[str, Any]],
    ) -> FinalizedTraceRun:
        """Finalize a trace run, capturing commits and managed fallback commits."""
        return await self.finalize_run_with_status(
            trace_run_id=trace_run_id,
            final_status="completed" if success else "failed",
            output=output,
            error=error,
            git_tool_events=git_tool_events,
        )

    async def finalize_run_with_status(
        self,
        *,
        trace_run_id: int,
        final_status: str,
        output: str,
        error: Optional[str],
        git_tool_events: list[dict[str, Any]],
    ) -> FinalizedTraceRun:
        """Finalize a trace run with an explicit terminal status."""
        trace_run = await self.db.get_trace_run(trace_run_id)
        if trace_run is None:
            raise RuntimeError(f"Trace run {trace_run_id} not found")

        trace_config = await self.get_config(trace_run.channel_id, trace_run.thread_ts)
        working_directory = trace_run.working_directory
        fallback_commit_hash: Optional[str] = None
        success = final_status == "completed"

        if (
            trace_config.auto_commit
            and success
            and trace_run.git_base_is_clean
            and await self._is_git_repo(working_directory)
        ):
            commit_diffs = await self.git_service.get_commit_diffs_since(
                working_directory,
                trace_run.git_base_commit,
            )
            if not commit_diffs:
                try:
                    status = await self.git_service.get_status(working_directory)
                except GitError:
                    status = None
                if status and status.has_changes():
                    fallback_commit_hash = await self._create_managed_commit(
                        working_directory=working_directory,
                        trace_run=trace_run,
                    )

        commits = await self._capture_trace_commits(trace_run, fallback_commit_hash)
        tool_events = await self._capture_git_tool_events(trace_run, git_tool_events)
        head_commit = None
        if commits:
            head_commit = commits[-1].commit_hash
        elif await self._is_git_repo(working_directory):
            try:
                head_commit = await self.git_service.get_head_commit_hash(working_directory)
            except GitError:
                head_commit = None

        summary = self._build_run_summary(
            trace_run=trace_run,
            final_status=final_status,
            output=output,
            error=error,
            commits=commits,
        )
        await self.db.update_trace_run(
            trace_run_id,
            status=final_status,
            git_head_commit=head_commit,
            summary=summary,
        )
        if final_status in {"failed", "cancelled"} and await self._is_git_repo(working_directory):
            try:
                status = await self.git_service.get_status(working_directory)
            except GitError:
                status = None
            if status and status.has_changes():
                checkpoint_name = self._build_checkpoint_name(trace_run)
                checkpoint = await self.git_service.create_checkpoint(
                    working_directory,
                    checkpoint_name,
                    description="Auto checkpoint after failed traced run",
                )
                await self.db.create_checkpoint(
                    trace_run.session_id,
                    trace_run.channel_id,
                    checkpoint_name,
                    checkpoint.stash_ref,
                    stash_message=checkpoint.message,
                    description=checkpoint.description,
                    is_auto=True,
                )
                await self.db.create_trace_event(
                    trace_run_id=trace_run_id,
                    channel_id=trace_run.channel_id,
                    thread_ts=trace_run.thread_ts,
                    event_type="failure_checkpoint_created",
                    payload={
                        "checkpoint_name": checkpoint_name,
                        "stash_ref": checkpoint.stash_ref,
                    },
                )

        await self.db.create_trace_event(
            trace_run_id=trace_run_id,
            channel_id=trace_run.channel_id,
            thread_ts=trace_run.thread_ts,
            event_type="run_finished",
            payload={
                "success": success,
                "final_status": final_status,
                "commit_count": len(commits),
                "fallback_commit_hash": fallback_commit_hash,
                "summary": summary,
            },
        )
        refreshed = await self.db.get_trace_run(trace_run_id)
        if refreshed is None:
            raise RuntimeError("Trace run disappeared after finalize")
        milestone_report = await self._complete_milestone_if_ready(trace_config, refreshed)
        openlineage_event_type = "COMPLETE"
        if final_status == "failed":
            openlineage_event_type = "FAIL"
        elif final_status == "cancelled":
            openlineage_event_type = "ABORT"
        self._schedule_openlineage_export(
            trace_config,
            refreshed,
            event_type=openlineage_event_type,
            commits=commits,
        )
        return FinalizedTraceRun(
            run=refreshed,
            commits=commits,
            tool_events=tool_events,
            milestone_report=milestone_report,
        )

    async def build_queue_summary(
        self,
        *,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        status_counts: dict[str, int],
        queue_item_ids: list[int],
        queue_drained: bool,
    ) -> QueueTraceReport:
        """Create queue-end and milestone aggregate reports when enabled."""
        trace_config = await self.get_config(channel_id, thread_ts)
        if not trace_config.enabled:
            return QueueTraceReport(queue_summary=None, milestone_reports=[])

        runs = await self.db.list_trace_runs_by_queue_items(queue_item_ids)
        commit_count = 0
        tool_event_count = 0
        milestone_names: list[str] = []
        milestones_by_id: dict[int, TraceMilestone] = {}
        milestone_reports: list[MilestoneReport] = []
        for run in runs:
            commit_count += len(await self.db.list_trace_commits(run.id))
            run_events = await self.db.list_trace_events(trace_run_id=run.id, limit=500)
            tool_event_count += sum(
                1 for event in run_events if event.event_type == "git_tool_event"
            )
            if run.milestone_id:
                milestone = await self.db.get_trace_milestone(run.milestone_id)
                if milestone is not None:
                    milestones_by_id[milestone.id] = milestone
                    if milestone.name not in milestone_names:
                        milestone_names.append(milestone.name)
        if queue_drained:
            for milestone in milestones_by_id.values():
                if milestone.status == "open" and milestone.mode != "explicit":
                    completed = await self.db.complete_trace_milestone(
                        milestone.id, summary="Queue drained"
                    )
                    if completed and trace_config.report_milestone:
                        refreshed = await self.db.get_trace_milestone(milestone.id)
                        if refreshed is not None:
                            milestone_reports.append(await self._build_milestone_report(refreshed))
        queue_summary = None
        if trace_config.report_queue_end:
            summary_text = (
                f"Queue trace summary: {status_counts.get('completed', 0)} completed, "
                f"{status_counts.get('failed', 0)} failed, "
                f"{status_counts.get('cancelled', 0)} cancelled, "
                f"{commit_count} commit(s) captured across {len(runs)} traced run(s)."
            )
            queue_summary = await self.db.create_trace_queue_summary(
                session_id=session_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                summary_text=summary_text,
                payload={
                    "status_counts": status_counts,
                    "run_ids": [run.id for run in runs],
                    "run_count": len(runs),
                    "milestones": milestone_names,
                    "commit_count": commit_count,
                    "tool_event_count": tool_event_count,
                },
            )
        return QueueTraceReport(queue_summary=queue_summary, milestone_reports=milestone_reports)

    async def preview_rollback(
        self,
        *,
        channel_id: str,
        thread_ts: Optional[str],
        working_directory: str,
        target_commit: str,
        trace_run_id: Optional[int] = None,
    ) -> tuple[RollbackEvent, RollbackPreview]:
        """Build and persist a rollback preview."""
        resolved_target = await self.resolve_commit(working_directory, target_commit)
        current_head = await self.git_service.get_head_commit_hash(working_directory)
        preview_key = self._build_rollback_preview_key(
            working_directory=working_directory,
            current_head=current_head,
            target_commit=resolved_target,
        )
        diff_text = await self.git_service.get_diff_between(
            working_directory,
            resolved_target,
            current_head or "HEAD",
            stat_only=True,
        )
        _, remote_url = await self.git_service.get_preferred_remote(working_directory)
        preview = RollbackPreview(
            target_commit=resolved_target,
            current_head=current_head,
            preview_key=preview_key,
            diff_text=diff_text,
            commit_url=self.git_service.build_commit_url(remote_url, resolved_target),
            compare_url=(
                self.git_service.build_compare_url(remote_url, resolved_target, current_head)
                if current_head
                else None
            ),
            already_at_target=current_head == resolved_target,
        )
        get_existing_preview = getattr(self.db, "get_latest_rollback_event_by_preview_key", None)
        rollback_event = None
        if get_existing_preview is not None:
            rollback_event = await get_existing_preview(
                channel_id,
                thread_ts,
                preview_key,
            )
        if rollback_event is None:
            rollback_event = await self.db.create_rollback_event(
                trace_run_id=trace_run_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                working_directory=working_directory,
                current_head_commit=current_head,
                target_commit=resolved_target,
                preview_key=preview_key,
                preview_diff=diff_text,
            )
        return rollback_event, preview

    async def apply_rollback(
        self,
        *,
        rollback_event_id: int,
        working_directory: str,
        session_id: int,
        channel_id: str,
        trace_run_id: Optional[int] = None,
    ) -> RollbackEvent:
        """Apply a previewed rollback."""
        rollback_event = await self.db.get_rollback_event(rollback_event_id)
        if rollback_event is None:
            raise RuntimeError(f"Rollback event {rollback_event_id} not found")
        if rollback_event.applied:
            return rollback_event

        target_working_directory = rollback_event.working_directory or working_directory
        current_head = None
        get_head_commit_hash = getattr(self.git_service, "get_head_commit_hash", None)
        if await self._is_git_repo(target_working_directory) and get_head_commit_hash is not None:
            current_head = await get_head_commit_hash(target_working_directory)
        if current_head == rollback_event.target_commit:
            await self.db.update_rollback_event(
                rollback_event_id,
                status="applied",
                applied=True,
                current_head_commit=current_head,
            )
            refreshed = await self.db.get_rollback_event(rollback_event_id)
            if refreshed is None:
                raise RuntimeError("Rollback event disappeared after idempotent apply")
            return refreshed

        checkpoint_name = None
        checkpoint_ref = None
        if await self._is_git_repo(target_working_directory):
            status = await self.git_service.get_status(target_working_directory)
            if status.has_changes():
                checkpoint_name = f"rollback-{rollback_event.target_commit[:8]}-{uuid4().hex[:8]}"
                checkpoint = await self.git_service.create_checkpoint(
                    target_working_directory,
                    checkpoint_name,
                    description="Safety checkpoint before rollback",
                )
                checkpoint_ref = checkpoint.stash_ref
                await self.db.create_checkpoint(
                    session_id,
                    channel_id,
                    checkpoint_name,
                    checkpoint.stash_ref,
                    stash_message=checkpoint.message,
                    description=checkpoint.description,
                    is_auto=True,
                )
        await self.git_service.reset_hard(
            target_working_directory,
            rollback_event.target_commit,
        )
        await self.db.update_rollback_event(
            rollback_event_id,
            status="applied",
            applied=True,
            current_head_commit=rollback_event.target_commit,
            checkpoint_name=checkpoint_name,
            checkpoint_ref=checkpoint_ref,
        )
        await self.db.create_trace_event(
            trace_run_id=trace_run_id,
            channel_id=channel_id,
            thread_ts=rollback_event.thread_ts,
            event_type="rollback_applied",
            payload={
                "target_commit": rollback_event.target_commit,
                "rollback_event_id": rollback_event_id,
                "checkpoint_name": checkpoint_name,
                "checkpoint_ref": checkpoint_ref,
            },
        )
        refreshed = await self.db.get_rollback_event(rollback_event_id)
        if refreshed is None:
            raise RuntimeError("Rollback event disappeared after apply")
        return refreshed

    async def resolve_commit(self, working_directory: str, revision: str) -> str:
        """Resolve a commit-ish to a full commit hash."""
        return await self.git_service.resolve_commit(working_directory, revision)

    async def _resolve_milestone(
        self,
        *,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        prompt: str,
        config_obj: TraceConfig,
        queue_item_id: Optional[int],
        root_key: Optional[str],
        milestone_name: Optional[str],
    ) -> Optional[TraceMilestone]:
        active_explicit = await self.db.get_active_explicit_trace_milestone(channel_id, thread_ts)
        if milestone_name:
            return await self.start_explicit_milestone(
                session_id=session_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                name=milestone_name,
            )

        if active_explicit is not None:
            return active_explicit

        if config_obj.milestone_mode == "explicit":
            return await self.db.get_open_trace_milestone(channel_id, thread_ts, root_key=root_key)

        if config_obj.milestone_mode == "fixed":
            open_milestone = await self.db.get_open_trace_milestone(
                channel_id, thread_ts, root_key=None
            )
            if open_milestone is not None and config_obj.milestone_batch_size:
                existing_runs = await self.db.list_trace_runs_for_milestone(open_milestone.id)
                if len(existing_runs) < config_obj.milestone_batch_size:
                    return open_milestone
                await self.db.complete_trace_milestone(
                    open_milestone.id, summary="Fixed batch complete"
                )

        inferred_root_key = root_key or (f"queue-item:{queue_item_id}" if queue_item_id else None)
        if inferred_root_key:
            existing = await self.db.get_open_trace_milestone(
                channel_id, thread_ts, root_key=inferred_root_key
            )
            if existing:
                return existing
        name = self._build_milestone_name(prompt)
        return await self.db.create_trace_milestone(
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            name=name,
            mode="inferred",
            root_key=inferred_root_key or f"prompt:{uuid4().hex[:8]}",
        )

    async def _capture_trace_commits(
        self,
        trace_run: TraceRun,
        fallback_commit_hash: Optional[str],
    ) -> list[TraceCommit]:
        if not await self._is_git_repo(trace_run.working_directory):
            return await self.db.replace_trace_commits(trace_run.id, [])

        commit_diffs = await self.git_service.get_commit_diffs_since(
            trace_run.working_directory,
            trace_run.git_base_commit,
        )
        commit_rows: list[dict[str, object]] = []
        for commit in commit_diffs:
            commit_rows.append(
                {
                    "commit_hash": commit.commit_hash,
                    "parent_hash": commit.parent_hash,
                    "short_hash": commit.short_hash,
                    "subject": commit.subject,
                    "author_name": commit.author_name,
                    "authored_at": commit.authored_at,
                    "commit_url": self.git_service.build_commit_url(
                        trace_run.remote_url, commit.commit_hash
                    ),
                    "compare_url": self.git_service.build_compare_url(
                        trace_run.remote_url, commit.parent_hash, commit.commit_hash
                    ),
                    "origin": "system" if fallback_commit_hash == commit.commit_hash else "model",
                    "diff": commit.diff,
                }
            )
        return await self.db.replace_trace_commits(trace_run.id, commit_rows)

    async def _capture_git_tool_events(
        self,
        trace_run: TraceRun,
        git_tool_events: list[dict[str, Any]],
    ) -> list[TraceEvent]:
        """Persist captured git-capable tool activity as trace events."""
        persisted_events: list[TraceEvent] = []
        for event in git_tool_events:
            tool_name = str(event.get("tool_name") or "").strip()
            if not tool_name:
                continue
            persisted_events.append(
                await self.db.create_trace_event(
                    trace_run_id=trace_run.id,
                    channel_id=trace_run.channel_id,
                    thread_ts=trace_run.thread_ts,
                    event_type="git_tool_event",
                    payload={
                        "tool_name": tool_name,
                        "summary": str(event.get("summary") or "").strip(),
                        "file_path": event.get("file_path"),
                        "commit_hash": event.get("commit_hash"),
                    },
                )
            )
        return persisted_events

    async def _create_managed_commit(
        self,
        *,
        working_directory: str,
        trace_run: TraceRun,
    ) -> Optional[str]:
        message = self._build_managed_commit_message(trace_run)
        try:
            await self.git_service.stage_all_changes(working_directory)
            return await self.git_service.commit_changes(working_directory, message)
        except GitError as exc:
            logger.error(f"Managed trace commit failed for run {trace_run.id}: {exc}")
            return None

    @staticmethod
    def _build_managed_commit_message(trace_run: TraceRun) -> str:
        prompt_summary = TraceService._compact_prompt(trace_run.prompt, limit=60)
        return (
            f"trace({trace_run.channel_id}): {prompt_summary}\n\n"
            f"Trace-Run: {trace_run.execution_id}\n"
            f"Backend: {trace_run.backend}\n"
            f"Queue-Item: {trace_run.queue_item_id or 'none'}"
        )

    @staticmethod
    def _build_checkpoint_name(trace_run: TraceRun) -> str:
        return f"trace-fail-{trace_run.execution_id[:8]}"

    @staticmethod
    def _compact_prompt(prompt: str, *, limit: int) -> str:
        compact = " ".join((prompt or "").split())
        if len(compact) <= limit:
            return compact or "update"
        return compact[: limit - 3] + "..."

    @staticmethod
    def _build_milestone_name(prompt: str) -> str:
        summary = TraceService._compact_prompt(prompt, limit=48)
        return summary or "Milestone"

    @staticmethod
    def _build_run_summary(
        *,
        trace_run: TraceRun,
        final_status: str,
        output: str,
        error: Optional[str],
        commits: list[TraceCommit],
    ) -> str:
        state = final_status
        verification_hint = (
            "verification mentioned"
            if "test" in (output or "").lower()
            else "no verification noted"
        )
        commit_part = f"{len(commits)} commit(s)" if commits else "no commits"
        return f"{state}: {commit_part}, {verification_hint}"

    @staticmethod
    def format_commit_snapshot(commits: list[TraceCommit]) -> tuple[Optional[str], Optional[str]]:
        """Build history-compatible git diff summary and detailed content."""
        if not commits:
            return None, None
        summary_items = [f"{commit.short_hash} {commit.subject}" for commit in commits]
        summary = f"{len(commits)} commit(s): " + "; ".join(summary_items)
        sections: list[str] = []
        for index, commit in enumerate(commits, start=1):
            sections.append(
                "\n".join(
                    [
                        f"## Commit {index}",
                        f"hash: {commit.commit_hash}",
                        f"parent_hash: {commit.parent_hash or '(none)'}",
                        f"short_hash: {commit.short_hash}",
                        f"message: {commit.subject}",
                        f"author: {commit.author_name}",
                        f"date: {commit.authored_at}",
                        f"origin: {commit.origin}",
                        "",
                        commit.diff or "(no diff)",
                    ]
                )
            )
        return summary, "\n\n".join(sections)

    async def _complete_milestone_if_ready(
        self,
        trace_config: TraceConfig,
        trace_run: TraceRun,
    ) -> Optional[MilestoneReport]:
        if trace_run.milestone_id is None:
            return None
        if trace_run.queue_item_id is None and trace_run.parent_run_id is None:
            completed = await self.db.complete_trace_milestone(
                trace_run.milestone_id, summary=trace_run.summary
            )
            if completed and trace_config.report_milestone:
                milestone = await self.db.get_trace_milestone(trace_run.milestone_id)
                if milestone is not None:
                    return await self._build_milestone_report(milestone)
        return None

    async def _build_milestone_report(self, milestone: TraceMilestone) -> MilestoneReport:
        list_runs = getattr(self.db, "list_trace_runs_for_milestone", None)
        if list_runs is None:
            runs = []
        else:
            runs = await list_runs(milestone.id)
        commits: list[TraceCommit] = []
        for run in runs:
            commits.extend(await self.db.list_trace_commits(run.id))
        summary_text = (
            f"Milestone `{milestone.name}` completed with {len(runs)} traced run(s) and "
            f"{len(commits)} commit(s)."
        )
        if milestone.summary:
            summary_text += f" Summary: {milestone.summary}"
        return MilestoneReport(
            milestone=milestone,
            runs=runs,
            commits=commits,
            summary_text=summary_text,
        )

    def _schedule_openlineage_export(
        self,
        trace_config: TraceConfig,
        trace_run: TraceRun,
        *,
        event_type: str,
        commits: Optional[list[TraceCommit]] = None,
    ) -> None:
        """Dispatch OpenLineage export without blocking the caller."""
        try:
            asyncio.create_task(
                self._emit_openlineage(
                    trace_config,
                    trace_run,
                    event_type=event_type,
                    commits=commits,
                )
            )
        except RuntimeError as exc:
            logger.error(f"Failed to schedule OpenLineage export for run {trace_run.id}: {exc}")

    async def _emit_openlineage(
        self,
        trace_config: TraceConfig,
        trace_run: TraceRun,
        *,
        event_type: str,
        commits: Optional[list[TraceCommit]] = None,
    ) -> None:
        if not trace_config.openlineage_enabled or not config.TRACE_OPENLINEAGE_ENABLED:
            return
        if not config.TRACE_OPENLINEAGE_URL:
            return
        payload = {
            "eventType": event_type,
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "run": {"runId": trace_run.execution_id},
            "job": {
                "namespace": config.TRACE_OPENLINEAGE_NAMESPACE or "slack-claude-code",
                "name": f"{trace_run.channel_id}:{trace_run.thread_ts or 'channel'}",
            },
            "producer": "slack-claude-code",
            "facets": {
                "slack": {
                    "_producer": "slack-claude-code",
                    "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/UnknownRunFacet.json",
                    "channelId": trace_run.channel_id,
                    "threadTs": trace_run.thread_ts,
                    "backend": trace_run.backend,
                    "model": trace_run.model,
                    "logicalRunId": trace_run.logical_run_id,
                    "attemptNumber": trace_run.attempt_number,
                    "queueItemId": trace_run.queue_item_id,
                    "commandId": trace_run.command_id,
                    "commitHashes": [commit.commit_hash for commit in commits or []],
                }
            },
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(config.TRACE_OPENLINEAGE_URL, json=payload) as response:
                    if response.status >= 400:
                        raise RuntimeError(
                            f"OpenLineage export failed with status {response.status}"
                        )
            await self.db.create_trace_event(
                trace_run_id=trace_run.id,
                channel_id=trace_run.channel_id,
                thread_ts=trace_run.thread_ts,
                event_type="openlineage_exported",
                payload={"event_type": event_type},
            )
        except Exception as exc:
            logger.error(f"OpenLineage export failed for run {trace_run.id}: {exc}")
            await self.db.create_trace_event(
                trace_run_id=trace_run.id,
                channel_id=trace_run.channel_id,
                thread_ts=trace_run.thread_ts,
                event_type="openlineage_failed",
                payload={"event_type": event_type, "error": str(exc)},
            )

    @staticmethod
    async def _is_git_repo(working_directory: str) -> bool:
        service = GitService()
        return await service.validate_git_repo(working_directory)

    @staticmethod
    def _build_rollback_preview_key(
        *,
        working_directory: str,
        current_head: Optional[str],
        target_commit: str,
    ) -> str:
        raw = f"{working_directory}\n{current_head or ''}\n{target_commit}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def stable_prompt_hash(prompt: str) -> str:
        """Return a deterministic prompt hash for lightweight lineage references."""
        return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()
