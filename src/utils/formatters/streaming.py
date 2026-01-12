"""Streaming message formatting."""

from typing import TYPE_CHECKING, Optional

from .base import escape_markdown, markdown_to_mrkdwn, truncate_from_start
from .tool_blocks import format_tool_activity_section

if TYPE_CHECKING:
    from src.claude.streaming import ToolActivity


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


def streaming_update(
    prompt: str,
    current_output: str,
    tool_activities: Optional[list["ToolActivity"]] = None,
    is_complete: bool = False,
    max_tools_display: int = 8,
) -> list[dict]:
    """Format a streaming update message with tool activity.

    Parameters
    ----------
    prompt : str
        The original user prompt.
    current_output : str
        The accumulated text output from Claude.
    tool_activities : list[ToolActivity], optional
        List of tool activities to display.
    is_complete : bool
        Whether the response is complete.
    max_tools_display : int
        Maximum number of tools to show in the activity section.

    Returns
    -------
    list[dict]
        Slack blocks for the streaming message.
    """
    status = (
        ":heavy_check_mark: Complete" if is_complete else ":arrows_counterclockwise: Streaming..."
    )

    # Truncate and convert to Slack mrkdwn format
    current_output = truncate_from_start(current_output)
    # Convert any standard markdown to Slack mrkdwn
    formatted_output = (
        markdown_to_mrkdwn(current_output) if current_output else "_Waiting for response..._"
    )

    blocks = [
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
            "text": {"type": "mrkdwn", "text": formatted_output},
        },
    ]

    # Add tool activity section if there are tools
    if tool_activities:
        tool_blocks = format_tool_activity_section(tool_activities, max_tools_display)
        blocks.extend(tool_blocks)

    return blocks
