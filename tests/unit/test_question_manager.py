"""Unit tests for question parsing and validation behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.question.manager import PendingQuestion, QuestionManager


def test_parse_ask_user_question_input_ignores_malformed_entries():
    """Malformed question/option payload entries should be ignored safely."""
    parsed = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                "invalid-question-entry",
                {
                    "id": 123,
                    "question": "Pick one",
                    "header": "Choice",
                    "options": [
                        "invalid-option-entry",
                        {"label": "A", "description": 5},
                    ],
                    "multiSelect": "yes",
                },
                {
                    "question": "Second question",
                    "header": "Second",
                    "options": "not-a-list",
                },
            ]
        }
    )

    assert len(parsed) == 2

    first = parsed[0]
    assert first.id == "123"
    assert first.question == "Pick one"
    assert first.header == "Choice"
    assert first.multi_select is True
    assert len(first.options) == 1
    assert first.options[0].label == "A"
    assert first.options[0].description == "5"

    second = parsed[1]
    assert second.question == "Second question"
    assert second.options == []
    assert second.multi_select is False


def test_parse_ask_user_question_input_handles_non_list_questions():
    """Non-list questions payloads should return an empty question list."""
    parsed = QuestionManager.parse_ask_user_question_input({"questions": "invalid"})
    assert parsed == []


def test_select_recommended_answers_prefers_recommended_suffix():
    """Auto-answer selection should prefer explicit `(Recommended)` options."""
    questions = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                {
                    "id": "q1",
                    "question": "Pick one",
                    "header": "Choice",
                    "options": [
                        {"label": "No"},
                        {"label": "Yes (Recommended)"},
                    ],
                    "multiSelect": False,
                },
                {
                    "id": "q2",
                    "question": "Pick many",
                    "header": "Multiple",
                    "options": [
                        {"label": "A (Recommended)"},
                        {"label": "B"},
                        {"label": "C (Recommended)"},
                    ],
                    "multiSelect": True,
                },
            ]
        }
    )

    answers = QuestionManager.select_recommended_answers(questions)
    assert answers == {0: ["Yes (Recommended)"], 1: ["A (Recommended)", "C (Recommended)"]}


def test_select_recommended_answers_falls_back_to_first_option():
    """When no recommendation marker exists, auto-answer should use the first option."""
    questions = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                {
                    "id": "q1",
                    "question": "Pick one",
                    "header": "Choice",
                    "options": [{"label": "First"}, {"label": "Second"}],
                    "multiSelect": False,
                },
                {
                    "id": "q2",
                    "question": "Pick many",
                    "header": "Multiple",
                    "options": [{"label": "A"}, {"label": "B"}],
                    "multiSelect": True,
                },
            ]
        }
    )

    answers = QuestionManager.select_recommended_answers(questions)
    assert answers == {0: ["First"], 1: ["A"]}


def test_normalize_question_tool_input_builds_single_question_payload():
    """Single-question payloads should normalize into canonical questions list."""
    normalized = QuestionManager.normalize_question_tool_input(
        {"question": "Proceed?", "header": "Confirm", "options": [{"label": "Yes"}]}
    )

    assert normalized == {
        "questions": [
            {
                "question": "Proceed?",
                "header": "Confirm",
                "options": [{"label": "Yes"}],
                "multiSelect": False,
            }
        ]
    }


def test_normalize_question_tool_input_applies_default_prompt():
    """Fallback defaults should create a usable canonical question payload."""
    normalized = QuestionManager.normalize_question_tool_input(
        {},
        default_question="Please provide additional input.",
        default_header="Input Needed",
    )

    assert normalized == {
        "questions": [
            {
                "question": "Please provide additional input.",
                "header": "Input Needed",
                "options": [],
                "multiSelect": False,
            }
        ]
    }


def test_serialize_answers_for_claude_and_codex():
    """Answer serialization should vary only by backend transport."""
    questions = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                {"id": "q1", "question": "Pick one", "header": "Choice", "options": []},
                {"id": "q2", "question": "Pick two", "header": "Second", "options": []},
            ]
        }
    )
    answers = {0: ["Yes"], 1: ["A", "B"]}

    assert QuestionManager.serialize_answers(questions, answers, backend="claude") == (
        "**Choice**: Yes\n**Second**: A, B"
    )
    assert QuestionManager.serialize_answers(questions, answers, backend="codex") == {
        "answers": {
            "q1": {"answers": ["Yes"]},
            "q2": {"answers": ["A", "B"]},
        }
    }


def test_question_mention_prefix_uses_configured_env_value():
    """Configured mention should always take precedence over auto-detection."""
    with patch("src.question.manager.config.SLACK_QUESTION_MENTION", "@here"):
        mention_prefix = QuestionManager._question_mention_prefix(
            context_text="Need input?",
            questions=[],
        )

    assert mention_prefix == "@here "


def test_question_mention_prefix_defaults_to_channel_on_question_context():
    """If mention env is empty, question-mark context should trigger @channel mention."""
    with patch("src.question.manager.config.SLACK_QUESTION_MENTION", ""):
        mention_prefix = QuestionManager._question_mention_prefix(
            context_text="Can you confirm this?",
            questions=[],
        )

    assert mention_prefix == "@channel "


def test_question_mention_prefix_defaults_to_channel_on_question_payload():
    """Question payload text should also trigger @channel fallback when env is empty."""
    questions = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                {
                    "question": "Proceed?",
                    "header": "Confirm",
                    "options": [{"label": "Yes"}],
                    "multiSelect": False,
                }
            ]
        }
    )
    with patch("src.question.manager.config.SLACK_QUESTION_MENTION", ""):
        mention_prefix = QuestionManager._question_mention_prefix(
            context_text="No explicit question context",
            questions=questions,
        )

    assert mention_prefix == "@channel "


def test_question_mention_prefix_stays_empty_without_question_detection():
    """No mention should be added when env is empty and no question text is detected."""
    with patch("src.question.manager.config.SLACK_QUESTION_MENTION", ""):
        mention_prefix = QuestionManager._question_mention_prefix(
            context_text="Status update only",
            questions=[],
        )

    assert mention_prefix == ""


@pytest.mark.asyncio
async def test_post_question_to_slack_defaults_to_channel_mention_when_detected():
    """Question posts and channel notifications should include @channel fallback mention."""
    pending = PendingQuestion(
        question_id="q123",
        session_id="session-1",
        channel_id="C1",
        thread_ts="123.456",
        tool_use_id="tool-1",
        questions=QuestionManager.parse_ask_user_question_input(
            {
                "questions": [
                    {
                        "question": "Proceed?",
                        "header": "Confirm",
                        "options": [{"label": "Yes"}],
                        "multiSelect": False,
                    }
                ]
            }
        ),
    )
    slack_client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "111.222"}))

    with patch("src.question.manager.config.SLACK_QUESTION_MENTION", ""):
        await QuestionManager.post_question_to_slack(
            pending=pending,
            slack_client=slack_client,
            db=None,
            context_text="Need a decision?",
        )

    first_message = slack_client.chat_postMessage.await_args_list[0].kwargs["text"]
    notification_message = slack_client.chat_postMessage.await_args_list[1].kwargs["text"]
    assert first_message.startswith("@channel ")
    assert notification_message.startswith("@channel ")
