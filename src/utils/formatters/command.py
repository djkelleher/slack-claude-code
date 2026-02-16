"""Command response formatting."""

from typing import Optional

from src.config import config

from .base import (
    FILE_THRESHOLD,
    escape_markdown,
    markdown_to_mrkdwn,
    sanitize_error,
    split_text_into_blocks,
    text_to_rich_text_blocks,
)
from .table import extract_tables_from_text, split_text_by_tables


def _split_blocks_by_limit(blocks: list[dict], max_blocks: int) -> list[list[dict]]:
    """Split a list of blocks into chunks that fit within Slack's per-message limit.

    Parameters
    ----------
    blocks : list[dict]
        Block Kit blocks to split.
    max_blocks : int
        Maximum number of blocks per message.

    Returns
    -------
    list[list[dict]]
        List of block arrays, each within the limit.
    """
    if len(blocks) <= max_blocks:
        return [blocks]
    chunks = []
    for start in range(0, len(blocks), max_blocks):
        chunks.append(blocks[start : start + max_blocks])
    return chunks


def command_response(
    prompt: str,
    output: str,
    command_id: Optional[int],
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_error: bool = False,
) -> list[dict]:
    """Format a command response using rich_text blocks for full-width display."""
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

    # Convert to rich_text blocks (renders at full width unlike section blocks)
    if output:
        output_blocks = text_to_rich_text_blocks(output)
        blocks.extend(output_blocks)
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}})

    # Add footer with metadata
    footer_parts = []
    if duration_ms is not None:
        footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
    if cost_usd is not None:
        footer_parts.append(f":moneybag: ${cost_usd:.4f}")
    if command_id is not None:
        footer_parts.append(f":memo: History #{command_id}")

    if footer_parts:
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
    # Extract a preview (first meaningful content, up to ~500 chars)
    preview = output.strip()
    if len(preview) > 500:
        # Try to break at a sentence boundary
        break_point = preview.rfind(". ", 0, 500)
        if break_point > 200:
            preview = preview[: break_point + 1]
        else:
            # Fall back to word boundary
            break_point = preview.rfind(" ", 0, 500)
            if break_point > 200:
                preview = preview[:break_point]
            else:
                preview = preview[:500]

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

    # Use rich_text blocks for full-width preview display
    if preview:
        preview_blocks = text_to_rich_text_blocks(preview)
        blocks.extend(preview_blocks)
        # Add truncation notice
        if len(output) > len(preview):
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_... (continued in thread)_"}}
            )
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}})

    # Add footer with metadata
    footer_parts = [f":speech_balloon: Full response in thread ({len(output):,} chars)"]
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
    # Extract tables from the output
    text_with_placeholders, table_blocks = extract_tables_from_text(output)

    # If no tables, return regular response in a list
    if not table_blocks:
        return [command_response(prompt, output, command_id, duration_ms, cost_usd, is_error)]

    # Split text by table placeholders
    segments = split_text_by_tables(text_with_placeholders)

    messages = []
    is_first = True

    max_blocks = config.SLACK_MAX_BLOCKS_PER_MESSAGE

    for segment in segments:
        if segment["type"] == "text":
            text_content = segment["content"]
            if not text_content or not text_content.strip():
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

            # Use rich_text blocks for full-width display
            output_blocks = text_to_rich_text_blocks(text_content)
            blocks.extend(output_blocks)

            # Split into multiple messages if block count exceeds Slack limit
            for chunk in _split_blocks_by_limit(blocks, max_blocks):
                messages.append(chunk)

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
        if duration_ms is not None:
            footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
        if cost_usd is not None:
            footer_parts.append(f":moneybag: ${cost_usd:.4f}")
        if command_id is not None:
            footer_parts.append(f":memo: History #{command_id}")

        if footer_parts:
            messages[-1].append({"type": "divider"})
            messages[-1].append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
                }
            )

    return messages if messages else [[{"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}}]]
