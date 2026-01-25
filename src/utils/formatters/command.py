"""Command response formatting."""

from typing import Optional

from .base import (
    FILE_THRESHOLD,
    escape_markdown,
    markdown_to_mrkdwn,
    sanitize_error,
    truncate_output,
)
from .table import extract_tables_from_text, split_text_by_tables

# Slack Block Kit limits
SLACK_TEXT_MAX_LENGTH = 3000


def _split_text_into_blocks(text: str, max_length: int = SLACK_TEXT_MAX_LENGTH) -> list[dict]:
    """Split long text into multiple Slack section blocks to preserve all content.

    Args:
        text: Text to split
        max_length: Maximum length per block (default: 3000 for Slack)

    Returns:
        List of Slack section blocks containing all the text
    """
    if len(text) <= max_length:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    blocks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunk = remaining
            remaining = ""
        else:
            # Find a good break point (newline or space) near the limit
            break_at = max_length
            # Try to break at a newline first
            newline_pos = remaining.rfind("\n", 0, max_length)
            if newline_pos > max_length // 2:
                break_at = newline_pos + 1
            else:
                # Fall back to breaking at a space
                space_pos = remaining.rfind(" ", 0, max_length)
                if space_pos > max_length // 2:
                    break_at = space_pos + 1

            chunk = remaining[:break_at].rstrip()
            remaining = remaining[break_at:].lstrip()

        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    return blocks


def command_response(
    prompt: str,
    output: str,
    command_id: Optional[int],
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_error: bool = False,
) -> list[dict]:
    """Format a command response."""
    # Convert standard markdown to Slack mrkdwn
    formatted_output = markdown_to_mrkdwn(output) if output else "_No output_"

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"> {escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                }
            ],
        },
        {"type": "divider"},
    ]

    # Split output into multiple blocks if needed
    output_blocks = _split_text_into_blocks(formatted_output)
    blocks.extend(output_blocks)

    # Add footer with metadata
    footer_parts = []
    if duration_ms:
        footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
    if cost_usd:
        footer_parts.append(f":moneybag: ${cost_usd:.4f}")
    if command_id is not None:
        footer_parts.append(f":memo: History #{command_id}")

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
        }
    )

    return blocks


def command_response_with_file(
    prompt: str,
    output: str,
    command_id: int,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_error: bool = False,
) -> tuple[list[dict], str, str]:
    """Format response with file attachment for large outputs.

    Returns
    -------
    tuple[list[dict], str, str]
        Tuple of (blocks, file_content, file_title)
    """
    # Extract a preview (first meaningful content)
    lines = output.strip().split("\n")
    preview_lines = []
    char_count = 0
    for line in lines:
        if char_count + len(line) > 500:
            break
        preview_lines.append(line)
        char_count += len(line)

    preview = "\n".join(preview_lines)
    if len(output) > len(preview):
        preview += "\n\n_... (see attached file for full response)_"

    # Convert preview to Slack mrkdwn
    formatted_preview = markdown_to_mrkdwn(preview) if preview else "_No output_"

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"> {escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": formatted_preview},
        },
    ]

    # Add footer with metadata
    footer_parts = [f":page_facing_up: Full response attached ({len(output):,} chars)"]
    if duration_ms:
        footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
    if cost_usd:
        footer_parts.append(f":moneybag: ${cost_usd:.4f}")
    footer_parts.append(f":memo: History #{command_id}")

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
        }
    )

    file_title = f"claude_response_{command_id}.txt"
    return blocks, output, file_title


def error_message(error: str) -> list[dict]:
    """Format an error message with sensitive information redacted."""
    sanitized = sanitize_error(error)
    # Don't escape content inside code blocks - Slack renders them literally
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":x: *Error*\n```{sanitized}```",
            },
        }
    ]


def should_attach_file(output: str) -> bool:
    """Check if output is large enough to warrant a file attachment."""
    return len(output) > FILE_THRESHOLD


def command_response_with_tables(
    prompt: str,
    output: str,
    command_id: Optional[int],
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_error: bool = False,
) -> list[list[dict]]:
    """Format a command response, splitting on tables.

    Each table must be sent as a separate message because Slack only allows
    one table block per message.

    Parameters
    ----------
    prompt : str
        The user's prompt.
    output : str
        The command output text.
    command_id : int
        The command history ID.
    duration_ms : int, optional
        Command duration in milliseconds.
    cost_usd : float, optional
        API cost in USD.
    is_error : bool
        Whether this is an error response.

    Returns
    -------
    list[list[dict]]
        List of block arrays, each representing a separate message.
        First message includes prompt context, last includes footer.
    """
    output = truncate_output(output)

    # Extract tables from the output
    text_with_placeholders, table_blocks = extract_tables_from_text(output)

    # If no tables, return regular response in a list
    if not table_blocks:
        return [command_response(prompt, output, command_id, duration_ms, cost_usd, is_error)]

    # Split text by table placeholders
    segments = split_text_by_tables(text_with_placeholders)

    messages = []
    is_first = True

    for segment in segments:
        if segment["type"] == "text":
            text_content = segment["content"]
            formatted_text = markdown_to_mrkdwn(text_content) if text_content else None

            if not formatted_text:
                continue

            blocks = []

            # Add prompt context to first message only
            if is_first:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"> {escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                            }
                        ],
                    }
                )
                blocks.append({"type": "divider"})
                is_first = False

            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": formatted_text},
                }
            )

            messages.append(blocks)

        elif segment["type"] == "table":
            table_block = table_blocks[segment["index"]]
            blocks = []

            # Add prompt context to first message only
            if is_first:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"> {escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                            }
                        ],
                    }
                )
                blocks.append({"type": "divider"})
                is_first = False

            blocks.append(table_block)
            messages.append(blocks)

    # Add footer to the last message
    if messages:
        footer_parts = []
        if duration_ms:
            footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
        if cost_usd:
            footer_parts.append(f":moneybag: ${cost_usd:.4f}")
        if command_id is not None:
            footer_parts.append(f":memo: History #{command_id}")

        messages[-1].append({"type": "divider"})
        messages[-1].append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
            }
        )

    return messages if messages else [[{"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}}]]
