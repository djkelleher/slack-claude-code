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

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{display_input}```",
            },
        })

    # Add context
    context_elements = [
        {
            "type": "mrkdwn",
            "text": f"Approval ID: `{approval_id}`",
        },
    ]

    if session_id:
        context_elements.append({
            "type": "mrkdwn",
            "text": f"Session: `{session_id[:8]}`",
        })

    blocks.append({
        "type": "context",
        "elements": context_elements,
    })

    # Add divider
    blocks.append({"type": "divider"})

    # Add approval buttons
    blocks.append({
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
    })

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
    emoji = ":white_check_mark:" if approved else ":x:"

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
        context_elements.append({
            "type": "mrkdwn",
            "text": f"By: <@{resolved_by}>",
        })

    blocks.append({
        "type": "context",
        "elements": context_elements,
    })

    return blocks
