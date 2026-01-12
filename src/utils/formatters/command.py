"""Command response formatting."""

from typing import Optional

from .base import (
    FILE_THRESHOLD,
    escape_markdown,
    markdown_to_mrkdwn,
    sanitize_error,
    truncate_output,
)


def command_response(
    prompt: str,
    output: str,
    command_id: int,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    is_error: bool = False,
) -> list[dict]:
    """Format a command response."""
    output = truncate_output(output)
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
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": formatted_output},
        },
    ]

    # Add footer with metadata
    footer_parts = []
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
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":x: *Error*\n```{escape_markdown(sanitized)}```",
            },
        }
    ]


def should_attach_file(output: str) -> bool:
    """Check if output is large enough to warrant a file attachment."""
    return len(output) > FILE_THRESHOLD
