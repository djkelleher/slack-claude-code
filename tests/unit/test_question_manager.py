"""Unit tests for question parsing and validation behavior."""

from src.question.manager import QuestionManager


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
