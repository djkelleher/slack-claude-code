"""Slack UI components for AskUserQuestion tool.

Builds interactive Block Kit elements for displaying questions
and capturing user responses.
"""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import PendingQuestion, Question


def build_question_blocks(pending: "PendingQuestion", context_text: str = "") -> list[dict]:
    """Build Slack blocks for displaying question(s).

    Args:
        pending: The pending question to display
        context_text: Optional context text from Claude explaining why they're asking

    Returns:
        List of Slack Block Kit blocks
    """
    blocks = []

    # Header
    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":question: Claude has a question",
                "emoji": True,
            },
        }
    )

    blocks.append({"type": "divider"})

    # Add context text if provided
    if context_text and context_text.strip():
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": context_text.strip(),
                },
            }
        )
        blocks.append({"type": "divider"})

    # Build blocks for each question
    for i, question in enumerate(pending.questions):
        # Question text
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{question.header}*\n{question.question}",
                },
            }
        )

        # Build action buttons for options
        if question.multi_select:
            # For multi-select, use checkboxes
            blocks.append(_build_checkbox_block(pending.question_id, i, question))
        else:
            # For single-select, use buttons
            blocks.append(_build_button_block(pending.question_id, i, question))

        # Add option descriptions if any
        descriptions = []
        for opt in question.options:
            if opt.description:
                descriptions.append(f"• *{opt.label}*: {opt.description}")

        if descriptions:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "\n".join(descriptions),
                        }
                    ],
                }
            )

        # Add spacing between questions
        if i < len(pending.questions) - 1:
            blocks.append({"type": "divider"})

    # Add submit button for multi-select questions
    has_multiselect = any(q.multi_select for q in pending.questions)
    if has_multiselect:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"question_submit_{pending.question_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Submit Selections",
                            "emoji": True,
                        },
                        "style": "primary",
                        "action_id": "question_multiselect_submit",
                        "value": pending.question_id,
                    }
                ],
            }
        )

    # Add "Other" text input option
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "_Or provide a custom answer:_",
            },
            "accessory": {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Custom Answer",
                    "emoji": True,
                },
                "action_id": "question_custom_answer",
                "value": pending.question_id,
            },
        }
    )

    return blocks


def _build_button_block(
    question_id: str,
    question_index: int,
    question: "Question",
) -> dict:
    """Build a button block for single-select question.

    Args:
        question_id: The question ID
        question_index: Index of this question
        question: The question object

    Returns:
        Slack actions block with buttons
    """
    buttons = []
    for opt in question.options:
        # Value encodes question_id, question_index, and selected label
        value = json.dumps(
            {
                "q": question_id,
                "i": question_index,
                "l": opt.label,
            }
        )

        buttons.append(
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": opt.label[:75],  # Slack button text limit
                    "emoji": True,
                },
                "action_id": f"question_select_{question_index}_{len(buttons)}",
                "value": value,
            }
        )

    return {
        "type": "actions",
        "block_id": f"question_actions_{question_id}_{question_index}",
        "elements": buttons[:5],  # Slack limit: 5 elements per actions block
    }


def _build_checkbox_block(
    question_id: str,
    question_index: int,
    question: "Question",
) -> dict:
    """Build a checkbox block for multi-select question.

    Args:
        question_id: The question ID
        question_index: Index of this question
        question: The question object

    Returns:
        Slack section block with checkboxes accessory
    """
    options = []
    for opt in question.options:
        option_dict = {
            "text": {
                "type": "mrkdwn",
                "text": f"*{opt.label}*",
            },
            "value": opt.label,
        }
        # Only add description if it's a non-empty string
        if opt.description:
            option_dict["description"] = {
                "type": "mrkdwn",
                "text": opt.description[:75],
            }
        options.append(option_dict)

    return {
        "type": "section",
        "block_id": f"question_checkbox_{question_id}_{question_index}",
        "text": {
            "type": "mrkdwn",
            "text": "_Select all that apply:_",
        },
        "accessory": {
            "type": "checkboxes",
            "action_id": f"question_multiselect_{question_index}",
            "options": options[:10],  # Slack limit: 10 options
        },
    }


def build_question_result_blocks(
    pending: "PendingQuestion",
    user_id: str,
) -> list[dict]:
    """Build blocks showing the answered question.

    Args:
        pending: The answered pending question
        user_id: User who answered

    Returns:
        List of Slack Block Kit blocks
    """
    blocks = []

    # Header showing answered
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":heavy_check_mark: *Question answered by <@{user_id}>*",
            },
        }
    )

    blocks.append({"type": "divider"})

    # Show each question and answer
    for i, question in enumerate(pending.questions):
        selected = pending.answers.get(i, ["(no answer)"])
        answer_text = ", ".join(selected)

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{question.header}*\n_{question.question}_\n\n*Answer:* {answer_text}",
                },
            }
        )

    return blocks


def build_custom_answer_modal(question_id: str) -> dict:
    """Build a modal for custom answer input.

    Args:
        question_id: The question ID

    Returns:
        Slack modal view
    """
    return {
        "type": "modal",
        "callback_id": "question_custom_submit",
        "private_metadata": question_id,
        "title": {
            "type": "plain_text",
            "text": "Custom Answer",
        },
        "submit": {
            "type": "plain_text",
            "text": "Submit",
        },
        "close": {
            "type": "plain_text",
            "text": "Cancel",
        },
        "blocks": [
            {
                "type": "input",
                "block_id": "custom_answer_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "custom_answer_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Type your answer here...",
                    },
                },
                "label": {
                    "type": "plain_text",
                    "text": "Your Answer",
                },
            },
        ],
    }
