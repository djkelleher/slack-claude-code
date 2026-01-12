"""Slack UI builders for permission approval messages."""

from typing import Optional


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

    # Add tool input if provided
    if tool_input:
        # Truncate long inputs
        display_input = tool_input[:500]
        if len(tool_input) > 500:
            display_input += "..."

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{display_input}```",
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
    plan_content: str,
    session_id: str,
) -> list[dict]:
    """Build Slack blocks for a plan approval request.

    Args:
        approval_id: Unique ID for this approval
        plan_content: The plan text to show user
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
                "text": "Claude has created an implementation plan. Please review and approve to continue with execution.",
            },
        },
    ]

    # Add plan content (truncated to 2000 chars to fit in Slack block)
    display_plan = plan_content[:2000]
    if len(plan_content) > 2000:
        display_plan += "\n\n... _(truncated, see full plan below)_"

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{display_plan}```",
            },
        }
    )

    # Add context
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Approval ID: `{approval_id}` | Session: `{session_id[:8]}`",
                },
            ],
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
        }
    )

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
