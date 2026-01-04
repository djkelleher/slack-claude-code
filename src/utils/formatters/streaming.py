"""Streaming message formatting."""

from .base import escape_markdown, truncate_from_start


def processing_message(prompt: str) -> list[dict]:
    """Format a 'processing' placeholder message."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":hourglass_flowing_sand: *Processing...*\n> {escape_markdown(prompt[:100])}{'...' if len(prompt) > 100 else ''}",
            },
        }
    ]


def streaming_update(prompt: str, current_output: str, is_complete: bool = False) -> list[dict]:
    """Format a streaming update message."""
    status = ":white_check_mark: Complete" if is_complete else ":arrows_counterclockwise: Streaming..."

    current_output = truncate_from_start(current_output)

    return [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{status}\n> {escape_markdown(prompt[:100])}{'...' if len(prompt) > 100 else ''}",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": current_output or "_Waiting for response..._"},
        },
    ]
