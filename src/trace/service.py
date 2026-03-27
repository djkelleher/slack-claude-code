"""Traceability, lineage, rollback, and reporting orchestration."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import aiohttp
from loguru import logger

from src.config import config
from src.database.models import (
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
    queue_summary_text: Optional[str] = None


@dataclass(frozen=True)
class RollbackPreview:
    """Computed rollback preview details."""

    target_commit: str
    current_head: Optional[str]
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
        git_branch = None
        remote_name = None
        remote_url = None
        if await self._is_git_repo(working_directory):
            try:
                git_base_commit = await self.git_service.get_head_commit_hash(working_directory)
                git_branch = await self.git_service.get_current_branch(working_directory)
                remote_name, remote_url = await self.git_service.get_preferred_remote(
                    working_directory
                )
            except GitError:
                git_base_commit = None
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
            execution_id=execution_id,
            backend=backend,
            model=model,
            working_directory=working_directory,
            prompt=prompt,
            git_base_commit=git_base_commit,
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
                "root_key": root_key,
            },
        )
        await self._emit_openlineage(trace_config, trace_run, event_type="START")
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
        trace_run = await self.db.get_trace_run(trace_run_id)
        if trace_run is None:
            raise RuntimeError(f"Trace run {trace_run_id} not found")

        trace_config = await self.get_config(trace_run.channel_id, trace_run.thread_ts)
        working_directory = trace_run.working_directory
        fallback_commit_hash: Optional[str] = None

        if trace_config.auto_commit and success and await self._is_git_repo(working_directory):
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
            success=success,
            output=output,
            error=error,
            commits=commits,
        )
        await self.db.update_trace_run(
            trace_run_id,
            status="completed" if success else "failed",
            git_head_commit=head_commit,
            summary=summary,
        )
        if not success and await self._is_git_repo(working_directory):
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
                "commit_count": len(commits),
                "fallback_commit_hash": fallback_commit_hash,
                "summary": summary,
            },
        )
        refreshed = await self.db.get_trace_run(trace_run_id)
        if refreshed is None:
            raise RuntimeError("Trace run disappeared after finalize")
        await self._complete_milestone_if_ready(refreshed)
        await self._emit_openlineage(
            trace_config,
            refreshed,
            event_type="COMPLETE" if success else "FAIL",
            commits=commits,
        )
        return FinalizedTraceRun(run=refreshed, commits=commits)

    async def build_queue_summary(
        self,
        *,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        status_counts: dict[str, int],
    ) -> Optional[TraceQueueSummary]:
        """Create an aggregated queue summary when trace reporting is enabled."""
        trace_config = await self.get_config(channel_id, thread_ts)
        if not trace_config.enabled or not trace_config.report_queue_end:
            return None

        runs = [
            run
            for run in await self.db.list_recent_trace_runs(channel_id, thread_ts, limit=50)
            if run.queue_item_id is not None
        ]
        commit_count = 0
        milestone_names: list[str] = []
        for run in runs:
            commit_count += len(await self.db.list_trace_commits(run.id))
            if run.milestone_id:
                milestone = await self.db.get_trace_milestone(run.milestone_id)
                if milestone and milestone.name not in milestone_names:
                    milestone_names.append(milestone.name)
        summary_text = (
            f"Queue trace summary: {status_counts.get('completed', 0)} completed, "
            f"{status_counts.get('failed', 0)} failed, "
            f"{status_counts.get('cancelled', 0)} cancelled, "
            f"{commit_count} commit(s) captured."
        )
        return await self.db.create_trace_queue_summary(
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            summary_text=summary_text,
            payload={
                "status_counts": status_counts,
                "run_ids": [run.id for run in runs],
                "milestones": milestone_names,
                "commit_count": commit_count,
            },
        )

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
            diff_text=diff_text,
            commit_url=self.git_service.build_commit_url(remote_url, resolved_target),
            compare_url=(
                self.git_service.build_compare_url(remote_url, resolved_target, current_head)
                if current_head
                else None
            ),
            already_at_target=current_head == resolved_target,
        )
        rollback_event = await self.db.create_rollback_event(
            trace_run_id=trace_run_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            target_commit=resolved_target,
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

        checkpoint_name = None
        checkpoint_ref = None
        if await self._is_git_repo(working_directory):
            status = await self.git_service.get_status(working_directory)
            if status.has_changes():
                checkpoint_name = f"rollback-{rollback_event.target_commit[:8]}-{uuid4().hex[:8]}"
                checkpoint = await self.git_service.create_checkpoint(
                    working_directory,
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
        await self.git_service.reset_hard(working_directory, rollback_event.target_commit)
        await self.db.update_rollback_event(
            rollback_event_id,
            status="applied",
            applied=True,
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
        if not config_obj.report_milestone:
            return None
        if milestone_name:
            existing = await self.db.get_open_trace_milestone(
                channel_id, thread_ts, root_key=root_key
            )
            if existing is not None:
                await self.db.complete_trace_milestone(
                    existing.id, summary="Closed by explicit marker"
                )
            return await self.db.create_trace_milestone(
                session_id=session_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                name=milestone_name,
                mode="explicit",
                root_key=root_key,
            )

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
        success: bool,
        output: str,
        error: Optional[str],
        commits: list[TraceCommit],
    ) -> str:
        state = "completed" if success else "failed"
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

    async def _complete_milestone_if_ready(self, trace_run: TraceRun) -> None:
        if trace_run.milestone_id is None:
            return
        if trace_run.queue_item_id is None and trace_run.parent_run_id is None:
            await self.db.complete_trace_milestone(
                trace_run.milestone_id, summary=trace_run.summary
            )

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
    def stable_prompt_hash(prompt: str) -> str:
        """Return a deterministic prompt hash for lightweight lineage references."""
        return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()
