"""Queue status formatting."""

from datetime import timezone
from typing import Any

from .base import escape_markdown


def _more_items_context(count: int) -> dict:
    """Render a standard overflow notice."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_... and {count} more_"}],
    }


def _escaped_preview(text: str, limit: int) -> str:
    """Escape and truncate text for compact queue previews."""
    if len(text) <= limit:
        return escape_markdown(text)
    head = max(1, int(limit * 0.65))
    tail = max(1, limit - head - 3)
    return escape_markdown(f"{text[:head]}...{text[-tail:]}")


def _running_item_label(item: Any) -> str:
    """Build a compact identifier for running queue items."""
    label = f"#{item.id}"
    if item.parallel_group_id:
        width = item.parallel_limit or "all"
        label += f" · parallel `{item.parallel_group_id}` (max {width})"
    return label


def _automation_prefix(item: Any) -> str:
    """Return a short queue label prefix for auto-generated items."""
    raw_meta = item.automation_meta
    if not isinstance(raw_meta, dict):
        return ""
    origin = str(raw_meta.get("origin") or "").strip().lower()
    if origin == "auto_check":
        return "[auto-check] "
    if origin == "auto_continue":
        return "[auto-continue] "
    return ""


def _pending_item_text(item: Any, displayed_position: int) -> str:
    """Render one pending queue line."""
    parallel_suffix = ""
    if item.parallel_group_id:
        parallel_suffix = f", parallel max {item.parallel_limit or 'all'}"
    return (
        f"*#{item.id}* (pos {displayed_position}{parallel_suffix})\n> "
        f"{_escaped_preview(_automation_prefix(item) + item.prompt, 100)}"
    )


def _scheduled_event_text(event: Any) -> str:
    """Render one pending scheduled queue event."""
    execute_at = event.execute_at
    if execute_at.tzinfo is None or execute_at.tzinfo.utcoffset(execute_at) is None:
        execute_at = execute_at.replace(tzinfo=timezone.utc)
    execute_at_utc = execute_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    event_id = event.id
    id_prefix = f"`#{event_id}` " if event_id is not None else ""
    return f":alarm_clock: {id_prefix}*{event.action}* at `{execute_at_utc}`"


def queue_status(pending: list, running: Any, scheduled_events: list | None = None) -> list[dict]:
    """Format queue status for /qv command."""
    running_items = running if isinstance(running, list) else ([running] if running else [])
    scheduled = scheduled_events or []
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":inbox_tray: Command Queue",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if running_items:
        running_lines = []
        for item in running_items[:10]:
            label = _running_item_label(item)
            running_lines.append(
                f":arrow_forward: *Running:* {label}\n> "
                f"{_escaped_preview(_automation_prefix(item) + item.prompt, 100)}"
            )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(running_lines)},
            }
        )
        blocks.append({"type": "divider"})

    if scheduled:
        lines = [_scheduled_event_text(event) for event in scheduled[:5]]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Scheduled Controls:*\n" + "\n".join(lines),
                },
            }
        )
        if len(scheduled) > 5:
            blocks.append(_more_items_context(len(scheduled) - 5))
        blocks.append({"type": "divider"})

    if not pending:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_Queue is empty_"},
            }
        )
    else:
        for index, item in enumerate(pending[:10], start=1):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _pending_item_text(item, index)},
                }
            )

        if len(pending) > 10:
            blocks.append(_more_items_context(len(pending) - 10))

    return blocks


def queue_item_running(item: Any, sequence_number: str) -> list[dict]:
    """Format running queue item status."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":arrow_forward: *Processing queue item {sequence_number}:*\n> "
                    f"{_escaped_preview(_automation_prefix(item) + ' '.join(item.prompt.split()), 200)}"
                ),
            },
        },
    ]


def queue_item_complete(item: Any, result: Any) -> list[dict]:
    """Format completed queue item."""
    status = ":heavy_check_mark:" if result.success else ":x:"
    output = result.output or result.error or "No output"
    if len(output) > 2500:
        output = output[:2500] + "\n\n... (truncated)"

    return [
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"{status} Queue Item #{item.id}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {_escaped_preview(_automation_prefix(item) + item.prompt, 100)}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output},
        },
    ]


def queue_scope_overview(scopes: list[dict[str, Any]]) -> list[dict]:
    """Format a channel-wide queue scope overview."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":inbox_tray: Queue Scopes",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if not scopes:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No queue activity found in this channel_",
                },
            }
        )
        return blocks

    for scope in scopes[:15]:
        summary_parts = [
            f"*State:* `{scope['state']}`",
            f"*Running:* {scope['running_count']}",
            f"*Pending:* {scope['pending_count']}",
        ]
        scheduled_count = int(scope.get("scheduled_count", 0))
        if scheduled_count:
            summary_parts.append(f"*Scheduled:* {scheduled_count}")
        text = f"*{escape_markdown(scope['label'])}*\n" + " | ".join(summary_parts)
        preview = scope.get("preview")
        if preview:
            text += f"\n> {_escaped_preview(preview, 120)}"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        )

    if len(scopes) > 15:
        blocks.append(_more_items_context(len(scopes) - 15))

    return blocks
