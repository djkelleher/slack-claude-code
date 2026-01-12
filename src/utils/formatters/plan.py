"""Plan mode response formatting."""

from typing import Optional

from .base import escape_markdown, truncate_output


def plan_processing_message(prompt: str) -> list[dict]:
    """Format initial planning message.

    Args:
        prompt: The user's prompt

    Returns:
        List of Slack blocks
    """
    return [
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
            "text": {
                "type": "mrkdwn",
                "text": ":hourglass_flowing_sand: *Creating implementation plan...*\n\nClaude is exploring the codebase and designing an approach.",
            },
        },
    ]


def plan_ready_message(
    prompt: str,
    plan_preview: str,
    approval_id: str,
) -> list[dict]:
    """Format message when plan is ready for review.

    Args:
        prompt: The user's prompt
        plan_preview: Preview of the plan content
        approval_id: The approval request ID

    Returns:
        List of Slack blocks
    """
    return [
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
            "text": {
                "type": "mrkdwn",
                "text": f":heavy_check_mark: *Plan created successfully*\n\nApproval ID: `{approval_id}`",
            },
        },
    ]


def plan_execution_update(
    prompt: str,
    current_output: str,
    duration_ms: Optional[int] = None,
) -> list[dict]:
    """Format execution progress message.

    Args:
        prompt: The user's prompt
        current_output: Current execution output
        duration_ms: Optional execution duration

    Returns:
        List of Slack blocks
    """
    output = truncate_output(current_output)

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
            "text": {
                "type": "mrkdwn",
                "text": ":gear: *Executing plan...*",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output or "_Starting execution..._"},
        },
    ]

    # Add footer with duration if available
    if duration_ms:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":stopwatch: {duration_ms / 1000:.1f}s"}],
            }
        )

    return blocks


def plan_execution_complete(
    prompt: str,
    output: str,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    command_id: Optional[int] = None,
) -> list[dict]:
    """Format plan execution completion message.

    Args:
        prompt: The user's prompt
        output: Final execution output
        duration_ms: Optional execution duration
        cost_usd: Optional execution cost
        command_id: Optional command history ID

    Returns:
        List of Slack blocks
    """
    output = truncate_output(output)

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
            "text": {"type": "mrkdwn", "text": output or "_No output_"},
        },
    ]

    # Add footer with metadata
    footer_parts = [":heavy_check_mark: Plan execution complete"]
    if duration_ms:
        footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
    if cost_usd:
        footer_parts.append(f":moneybag: ${cost_usd:.4f}")
    if command_id:
        footer_parts.append(f":memo: History #{command_id}")

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
        }
    )

    return blocks
