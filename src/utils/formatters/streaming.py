"""Streaming message formatting."""

from typing import TYPE_CHECKING, Optional

from .base import (
    MAX_TEXT_LENGTH,
    escape_markdown,
    text_to_rich_text_blocks,
    truncate_from_start,
)
from .tool_blocks import format_tool_activity_section

if TYPE_CHECKING:
    from src.claude.sdk_stream_adapter import ToolActivity


_ELLIPSIS = "..."
_PROCESSING_PREFIX = ":hourglass_flowing_sand: *Processing...*\n> "
_PROMPT_SECTION_PREFIX = "> "


def _truncate_preview(text: str, max_length: int) -> str:
    """Truncate preview text to a Slack-safe length."""
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    if max_length <= len(_ELLIPSIS):
        return _ELLIPSIS[:max_length]
    return text[: max_length - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _format_prompt_preview(prompt: str, prefix: str) -> str:
    """Normalize, escape, and truncate a prompt preview for a mrkdwn block."""
    prompt_text = escape_markdown(" ".join(prompt.split()))
    return _truncate_preview(prompt_text, MAX_TEXT_LENGTH - len(prefix))


def processing_fallback_text(prompt: str) -> str:
    """Build plain-text fallback content for Slack message payloads."""
    prompt_text = " ".join(prompt.split())
    if not prompt_text:
        return "Processing..."
    return _truncate_preview(f"Processing: {prompt_text}", 300)


def processing_message(prompt: str) -> list[dict]:
    """Format a 'processing' placeholder message."""
    prompt_text = _format_prompt_preview(prompt, _PROCESSING_PREFIX)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{_PROCESSING_PREFIX}{prompt_text}",
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
    terminal_style: bool = False,
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
    terminal_style : bool
        Preserve single line breaks for terminal-like rendering.

    Returns
    -------
    list[dict]
        Slack blocks for the streaming message.
    """
    prompt_text = _format_prompt_preview(prompt, _PROMPT_SECTION_PREFIX)

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
                "text": f"> {prompt_text}",
            },
        },
        {"type": "divider"},
    ]

    # Convert to rich_text blocks (renders at full width unlike section blocks)
    if current_output:
        output_blocks = text_to_rich_text_blocks(current_output, terminal_style=terminal_style)
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
