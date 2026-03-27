"""Slack formatting for traceability, lineage, and rollback views."""

from typing import Optional

from src.database.models import (
    RollbackEvent,
    TraceCommit,
    TraceConfig,
    TraceEvent,
    TraceMilestone,
    TraceQueueSummary,
    TraceRun,
)
from src.trace.service import MilestoneReport, RollbackPreview

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
    tool_events: Optional[list[TraceEvent]] = None,
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
    tool_lines: list[str] = []
    for event in tool_events or []:
        summary = str(event.payload.get("summary") or "").strip()
        tool_name = str(event.payload.get("tool_name") or "git").strip()
        file_path = str(event.payload.get("file_path") or "").strip()
        line = f"`{escape_markdown(tool_name)}`"
        if summary:
            line += f" {escape_markdown(summary)}"
        if file_path:
            line += f" ({escape_markdown(file_path)})"
        tool_lines.append(line)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Trace Step* `{run.execution_id}`\n"
                    f"*Status:* `{run.status}` | *Backend:* `{run.backend}`"
                    f"{f' | *Model:* `{run.model}`' if run.model else ''}"
                    f"\n*Logical Run:* `{run.logical_run_id}` | *Attempt:* `{run.attempt_number}`"
                    f"{milestone_text}\n"
                    f"*Prompt:* {escape_markdown(' '.join(run.prompt.split())[:140])}"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Commits:*\n" + "\n".join(commit_lines)},
        },
        *(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Git Tool Activity:*\n" + "\n".join(tool_lines[:8]),
                    },
                }
            ]
            if tool_lines
            else []
        ),
    ]


def trace_lineage_blocks(
    run: TraceRun,
    commits: list[TraceCommit],
    events_count: int,
    milestone: Optional[TraceMilestone] = None,
    related_runs: Optional[list[TraceRun]] = None,
    related_run_commit_counts: Optional[dict[int, int]] = None,
    related_run_event_counts: Optional[dict[int, int]] = None,
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
    if run.logical_run_id:
        lines.append(f"*Logical Run:* `{run.logical_run_id}`")
        lines.append(f"*Attempt:* `{run.attempt_number}`")
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
        *(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Attempts:*\n"
                        + "\n".join(
                            _format_related_run_line(
                                related_run,
                                related_run_commit_counts or {},
                                related_run_event_counts or {},
                            )
                            for related_run in related_runs[:12]
                        ),
                    },
                }
            ]
            if related_runs and len(related_runs) > 1
            else []
        ),
    ]


def _format_related_run_line(
    run: TraceRun,
    commit_counts: dict[int, int],
    event_counts: dict[int, int],
) -> str:
    """Render one attempt summary line for lineage views."""
    return (
        f"- attempt `{run.attempt_number}` | status `{run.status}` | "
        f"execution `{run.execution_id}` | commits {commit_counts.get(run.id or 0, 0)} | "
        f"events {event_counts.get(run.id or 0, 0)}"
    )


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
                    f"*Preview Key:* `{preview.preview_key[:12]}`\n"
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
    run_count = int(payload.get("run_count") or len(payload.get("run_ids") or []))
    commit_count = int(payload.get("commit_count") or 0)
    tool_event_count = int(payload.get("tool_event_count") or 0)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Queue Trace Summary*\n{escape_markdown(summary.summary_text)}\n"
                    f"*Runs:* {run_count} | *Commits:* {commit_count} | "
                    f"*Git Tool Events:* {tool_event_count}\n"
                    f"*Milestones:* {escape_markdown(milestone_text)}"
                ),
            },
        }
    ]


def milestone_report_blocks(report: MilestoneReport) -> list[dict]:
    """Render an aggregate milestone report."""
    run_count = len(report.runs)
    commit_count = len(report.commits)
    commit_lines = [
        f"- `{commit.short_hash}` {escape_markdown(commit.subject)}"
        for commit in report.commits[:12]
    ]
    if not commit_lines:
        commit_lines = ["- none"]
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Milestone Report* `{report.milestone.id}`\n"
                    f"*Name:* {escape_markdown(report.milestone.name)}\n"
                    f"*Status:* `{report.milestone.status}` | *Runs:* {run_count} | "
                    f"*Commits:* {commit_count}\n"
                    f"{escape_markdown(report.summary_text)}"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Commits:*\n" + "\n".join(commit_lines)},
        },
    ]
