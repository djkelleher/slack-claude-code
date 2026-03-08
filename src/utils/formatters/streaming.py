"""Streaming message formatting."""

from typing import TYPE_CHECKING, Optional

from .base import escape_markdown, text_to_rich_text_blocks, truncate_from_start
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
    is_error: bool = False,
    max_tools_display: int = 8,
    truncate_output: bool = True,
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
    is_error : bool
        Whether the execution completed with an error.
    max_tools_display : int
        Maximum number of tools to show in the activity section.
    truncate_output : bool
        Whether to trim earlier output before splitting into Slack blocks.

    Returns
    -------
    list[dict]
        Slack blocks for the streaming message.
    """
    prompt_preview = f"{escape_markdown(prompt[:100])}{'...' if len(prompt) > 100 else ''}"

    if is_complete:
        status = ":x: Failed" if is_error else ":heavy_check_mark: Complete"
    else:
        status = ":arrows_counterclockwise: Streaming..."

    if truncate_output:
        current_output = truncate_from_start(current_output)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": status,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {prompt_preview}",
            },
        },
        {"type": "divider"},
    ]

    # Convert to rich_text blocks (renders at full width unlike section blocks)
    if current_output:
        output_blocks = text_to_rich_text_blocks(current_output)
        blocks.extend(output_blocks)
    else:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Waiting for response..._"}}
        )

    # Add tool activity section if there are tools
    if tool_activities:
        tool_blocks = format_tool_activity_section(tool_activities, max_tools_display)
        blocks.extend(tool_blocks)

    return blocks
