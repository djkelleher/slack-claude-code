"""Slack formatting for traceability, lineage, and rollback views."""

from typing import Optional

from src.database.models import (
    RollbackEvent,
    TraceCommit,
    TraceConfig,
    TraceMilestone,
    TraceQueueSummary,
    TraceRun,
)
from src.trace.service import RollbackPreview

from .base import escape_markdown


def trace_config_blocks(config: TraceConfig) -> list[dict]:
    """Render current trace configuration."""
    status = "enabled" if config.enabled else "disabled"
    reports = []
    if config.report_tool:
        reports.append("tool")
    if config.report_step:
        reports.append("step")
    if config.report_milestone:
        reports.append("milestone")
    if config.report_queue_end:
        reports.append("queue-end")
    report_text = ", ".join(reports) if reports else "none"
    milestone_text = config.milestone_mode
    if config.milestone_mode == "fixed" and config.milestone_batch_size:
        milestone_text = f"{milestone_text} ({config.milestone_batch_size})"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Traceability:* `{status}`\n"
                    f"*Auto commit:* `{config.auto_commit}`\n"
                    f"*Reports:* {report_text}\n"
                    f"*Milestones:* `{milestone_text}`\n"
                    f"*OpenLineage:* `{config.openlineage_enabled}`"
                ),
            },
        }
    ]


def trace_step_report_blocks(
    run: TraceRun,
    commits: list[TraceCommit],
    milestone: Optional[TraceMilestone] = None,
) -> list[dict]:
    """Render one step-level trace report."""
    commit_lines = ["_No commits captured for this run._"]
    if commits:
        commit_lines = []
        for commit in commits[:8]:
            line = f"`{commit.short_hash}` {escape_markdown(commit.subject)}"
            if commit.commit_url:
                line += f" <{commit.commit_url}|view>"
            if commit.compare_url:
                line += f" <{commit.compare_url}|compare>"
            commit_lines.append(line)
    milestone_text = ""
    if milestone is not None:
        milestone_text = f"\n*Milestone:* {escape_markdown(milestone.name)}"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Trace Step* `{run.execution_id}`\n"
                    f"*Status:* `{run.status}` | *Backend:* `{run.backend}`"
                    f"{f' | *Model:* `{run.model}`' if run.model else ''}"
                    f"{milestone_text}\n"
                    f"*Prompt:* {escape_markdown(' '.join(run.prompt.split())[:140])}"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Commits:*\n" + "\n".join(commit_lines)},
        },
    ]


def trace_lineage_blocks(
    run: TraceRun,
    commits: list[TraceCommit],
    events_count: int,
    milestone: Optional[TraceMilestone] = None,
) -> list[dict]:
    """Render lineage details for one run."""
    lines = [
        f"*Execution:* `{run.execution_id}`",
        f"*Status:* `{run.status}`",
        f"*Backend:* `{run.backend}`",
    ]
    if run.model:
        lines.append(f"*Model:* `{run.model}`")
    if run.command_id:
        lines.append(f"*Command:* `#{run.command_id}`")
    if run.queue_item_id:
        lines.append(f"*Queue Item:* `#{run.queue_item_id}`")
    if run.parent_run_id:
        lines.append(f"*Parent Run:* `#{run.parent_run_id}`")
    if run.root_run_id:
        lines.append(f"*Root Run:* `#{run.root_run_id}`")
    if milestone is not None:
        lines.append(f"*Milestone:* {escape_markdown(milestone.name)}")
    lines.append(f"*Events:* {events_count}")
    commit_text = (
        "\n".join(
            f"- `{commit.short_hash}` {escape_markdown(commit.subject)}" for commit in commits[:12]
        )
        or "- none"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Commits:*\n" + commit_text}},
    ]


def rollback_preview_blocks(event: RollbackEvent, preview: RollbackPreview) -> list[dict]:
    """Render rollback preview and apply button."""
    compare_link = ""
    if preview.compare_url:
        compare_link = f"\n<{preview.compare_url}|Compare on GitHub>"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Rollback Preview* `#{event.id}`\n"
                    f"*Target:* `{preview.target_commit}`\n"
                    f"*Current HEAD:* `{preview.current_head or 'unknown'}`\n"
                    f"{preview.diff_text}{compare_link}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Apply Rollback", "emoji": True},
                    "style": "danger",
                    "action_id": "rollback_apply",
                    "value": str(event.id),
                }
            ],
        },
    ]


def queue_trace_summary_blocks(summary: TraceQueueSummary) -> list[dict]:
    """Render a queue-level trace summary."""
    payload = summary.payload or {}
    milestones = payload.get("milestones") or []
    milestone_text = ", ".join(str(item) for item in milestones[:8]) if milestones else "none"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Queue Trace Summary*\n{escape_markdown(summary.summary_text)}\n"
                    f"*Milestones:* {escape_markdown(milestone_text)}"
                ),
            },
        }
    ]
