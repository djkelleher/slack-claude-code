"""Queue status formatting."""

from typing import Any

from .base import escape_markdown


def _escaped_preview(text: str, limit: int) -> str:
    """Escape and truncate text for compact queue previews."""
    suffix = "..." if len(text) > limit else ""
    return f"{escape_markdown(text[:limit])}{suffix}"


def _running_item_label(item: Any) -> str:
    """Build a compact identifier for running queue items."""
    label = f"#{item.id}"
    if item.parallel_group_id:
        width = item.parallel_limit or "all"
        label += f" · parallel `{item.parallel_group_id}` (max {width})"
    return label


def _pending_item_text(item: Any) -> str:
    """Render one pending queue line."""
    parallel_suffix = ""
    if item.parallel_group_id:
        parallel_suffix = f", parallel max {item.parallel_limit or 'all'}"
    return (
        f"*#{item.id}* (pos {item.position}{parallel_suffix})\n> "
        f"{_escaped_preview(item.prompt, 100)}"
    )


def queue_status(pending: list, running: Any) -> list[dict]:
    """Format queue status for /qv command."""
    running_items = running if isinstance(running, list) else ([running] if running else [])
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
                f":arrow_forward: *Running:* {label}\n> " f"{_escaped_preview(item.prompt, 100)}"
            )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(running_lines)},
            }
        )
        blocks.append({"type": "divider"})

    if not pending:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_Queue is empty_"},
            }
        )
    else:
        for item in pending[:10]:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _pending_item_text(item)},
                }
            )

        if len(pending) > 10:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"_... and {len(pending) - 10} more_"}],
                }
            )

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
                    f"{_escaped_preview(item.prompt, 200)}"
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
                "text": f"> {_escaped_preview(item.prompt, 100)}",
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
                "text": {"type": "mrkdwn", "text": "_No queue activity found in this channel_"},
            }
        )
        return blocks

    for scope in scopes[:15]:
        summary_parts = [
            f"*State:* `{scope['state']}`",
            f"*Running:* {scope['running_count']}",
            f"*Pending:* {scope['pending_count']}",
        ]
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
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_... and {len(scopes) - 15} more_"}],
            }
        )

    return blocks
