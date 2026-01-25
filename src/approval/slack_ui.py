"""Slack UI builders for permission approval messages."""

from typing import Optional

from src.utils.formatters.base import MAX_TEXT_LENGTH, split_text_into_blocks


def build_approval_blocks(
    approval_id: str,
    tool_name: str,
    tool_input: Optional[str] = None,
    session_id: Optional[str] = None,
) -> list[dict]:
    """Build Slack blocks for an approval request message.

    Args:
        approval_id: Unique ID for this approval
        tool_name: Name of the tool requesting permission
        tool_input: Optional tool input/arguments
        session_id: Optional session ID for context

    Returns:
        List of Slack block kit blocks
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Permission Required",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Claude wants to use *{tool_name}*",
            },
        },
    ]

    # Add tool input if provided (split into multiple blocks if needed)
    if tool_input:
        # Wrap in code block - account for ``` markers in length calculation
        code_block_overhead = 6  # ``` at start and end
        max_code_length = MAX_TEXT_LENGTH - code_block_overhead

        if len(tool_input) <= max_code_length:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{tool_input}```",
                    },
                }
            )
        else:
            # Split long input across multiple code blocks
            remaining = tool_input
            while remaining:
                if len(remaining) <= max_code_length:
                    chunk = remaining
                    remaining = ""
                else:
                    # Find a good break point at a newline
                    break_at = max_code_length
                    newline_pos = remaining.rfind("\n", 0, max_code_length)
                    if newline_pos > max_code_length // 2:
                        break_at = newline_pos + 1
                    chunk = remaining[:break_at].rstrip()
                    remaining = remaining[break_at:].lstrip()

                if chunk:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"```{chunk}```",
                            },
                        }
                    )

    # Add context
    context_elements = [
        {
            "type": "mrkdwn",
            "text": f"Approval ID: `{approval_id}`",
        },
    ]

    if session_id:
        context_elements.append(
            {
                "type": "mrkdwn",
                "text": f"Session: `{session_id[:8]}`",
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": context_elements,
        }
    )

    # Add divider
    blocks.append({"type": "divider"})

    # Add approval buttons
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve",
                        "emoji": True,
                    },
                    "style": "primary",
                    "value": approval_id,
                    "action_id": "approve_tool",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Deny",
                        "emoji": True,
                    },
                    "style": "danger",
                    "value": approval_id,
                    "action_id": "deny_tool",
                },
            ],
        }
    )

    return blocks


def build_approval_result_blocks(
    approval_id: str,
    tool_name: str,
    approved: bool,
    resolved_by: Optional[str] = None,
) -> list[dict]:
    """Build Slack blocks for an approval result (after user responds).

    Args:
        approval_id: The approval ID
        tool_name: Name of the tool
        approved: Whether it was approved
        resolved_by: User who resolved

    Returns:
        List of Slack block kit blocks
    """
    status = "Approved" if approved else "Denied"
    emoji = ":heavy_check_mark:" if approved else ":x:"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{status}*: {tool_name}",
            },
        },
    ]

    context_elements = [
        {
            "type": "mrkdwn",
            "text": f"Approval ID: `{approval_id}`",
        },
    ]

    if resolved_by:
        context_elements.append(
            {
                "type": "mrkdwn",
                "text": f"By: <@{resolved_by}>",
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": context_elements,
        }
    )

    return blocks


def build_plan_approval_blocks(
    approval_id: str,
    session_id: str,
) -> list[dict]:
    """Build Slack blocks for a plan approval request.

    The plan content is attached as a file snippet separately, so these blocks
    only contain the header, description, and approval buttons.

    Args:
        approval_id: Unique ID for this approval
        session_id: Session ID for context

    Returns:
        List of Slack block kit blocks
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📋 Plan Ready for Review",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Claude has created an implementation plan. Review the attached plan file and approve to continue with execution.",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Approval ID: `{approval_id}` | Session: `{session_id[:8]}`",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Approve Plan",
                        "emoji": True,
                    },
                    "style": "primary",
                    "value": approval_id,
                    "action_id": "approve_plan",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ Reject Plan",
                        "emoji": True,
                    },
                    "style": "danger",
                    "value": approval_id,
                    "action_id": "reject_plan",
                },
            ],
        },
    ]

    return blocks


def build_plan_result_blocks(
    approval_id: str,
    approved: bool,
    user_id: str,
) -> list[dict]:
    """Build Slack blocks for plan approval result.

    Args:
        approval_id: The approval ID
        approved: Whether it was approved
        user_id: User who resolved

    Returns:
        List of Slack block kit blocks
    """
    if approved:
        status = "✅ Plan Approved"
        message = "Proceeding with execution..."
    else:
        status = "❌ Plan Rejected"
        message = "Execution cancelled."

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{status}*\n{message}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Approval ID: `{approval_id}` | By: <@{user_id}>",
                },
            ],
        },
    ]

    return blocks
