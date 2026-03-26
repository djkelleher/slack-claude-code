"""Unit tests for queue auto-follow decision helpers."""

import pytest

from src.handlers.claude.queue_automation import (
    TaskStatusReport,
    build_check_prompts,
    build_continue_prompt,
    build_task_status_suffix,
    decide_queue_automation,
    parse_task_status_block,
)


@pytest.mark.asyncio
async def test_decide_queue_automation_detects_textual_continue_signal() -> None:
    decision = await decide_queue_automation(
        prompt="Implement feature X",
        output="Next steps: wire the remaining handler and run tests.",
        detailed_output="",
        git_tool_events=[],
        judge_runner=None,
    )

    assert decision.should_continue is True


@pytest.mark.asyncio
async def test_decide_queue_automation_respects_done_signal_without_other_signals() -> None:
    decision = await decide_queue_automation(
        prompt="Implement feature X",
        output="All done. Nothing left to do.",
        detailed_output="",
        git_tool_events=[],
        judge_runner=None,
    )

    assert decision.should_continue is False


@pytest.mark.asyncio
async def test_decide_queue_automation_uses_llm_judge_payload() -> None:
    async def _judge_runner(_: str) -> str:
        return '{"remaining_work": true, "confidence": 0.9, "math_heavy": true, "reason": "todo"}'

    decision = await decide_queue_automation(
        prompt="Implement feature X",
        output="Status summary.",
        detailed_output="",
        git_tool_events=[],
        judge_runner=_judge_runner,
    )

    assert decision.judge_used is True
    assert decision.should_continue is True
    assert decision.include_math_check is True


def test_build_check_prompts_appends_math_prompt_when_requested() -> None:
    prompts = build_check_prompts(include_math_check=True)

    assert len(prompts) == 4
    assert "math-heavy" in prompts[-1].lower()


def test_build_continue_prompt_returns_non_empty_prompt() -> None:
    prompt = build_continue_prompt()
    assert "remaining" in prompt.lower()


# ---------------------------------------------------------------------------
# parse_task_status_block tests
# ---------------------------------------------------------------------------


class TestParseTaskStatusBlock:
    """Tests for structured <task-status> block parsing."""

    def test_returns_none_for_empty_string(self) -> None:
        assert parse_task_status_block("") is None

    def test_returns_none_when_no_block_present(self) -> None:
        output = "I finished all the work. Everything looks good."
        assert parse_task_status_block(output) is None

    def test_parses_complete_short_form(self) -> None:
        output = "All work is done.\n\n" "<task-status>\n" "status: complete\n" "</task-status>"
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is True
        assert report.original_tasks == []
        assert report.discovered_tasks == []

    def test_parses_incomplete_with_original_tasks(self) -> None:
        output = (
            "Made progress on the implementation.\n\n"
            "<task-status>\n"
            "status: incomplete\n"
            "\n"
            "[original-plan]\n"
            "- DONE | Implement the login endpoint\n"
            "- INCOMPLETE | Add rate limiting to the login endpoint\n"
            "- INCOMPLETE | Write tests for the login endpoint\n"
            "</task-status>"
        )
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is False
        assert len(report.original_tasks) == 3
        assert report.original_tasks[0] == ("DONE", "Implement the login endpoint")
        assert report.original_tasks[1] == ("INCOMPLETE", "Add rate limiting to the login endpoint")
        assert report.original_tasks[2] == ("INCOMPLETE", "Write tests for the login endpoint")
        assert report.discovered_tasks == []

    def test_parses_incomplete_with_discovered_tasks(self) -> None:
        output = (
            "<task-status>\n"
            "status: incomplete\n"
            "\n"
            "[original-plan]\n"
            "- DONE | Build the API\n"
            "\n"
            "[discovered]\n"
            "- CRITICAL | Fix broken CORS middleware\n"
            "- HIGH | Refactor auth helper to reduce duplication\n"
            "- MEDIUM | Add docstrings to new functions\n"
            "- LOW | Clean up unused imports\n"
            "</task-status>"
        )
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is False
        assert len(report.original_tasks) == 1
        assert report.original_tasks[0] == ("DONE", "Build the API")
        assert len(report.discovered_tasks) == 4
        assert report.discovered_tasks[0] == ("CRITICAL", "Fix broken CORS middleware")
        assert report.discovered_tasks[1] == ("HIGH", "Refactor auth helper to reduce duplication")
        assert report.discovered_tasks[2] == ("MEDIUM", "Add docstrings to new functions")
        assert report.discovered_tasks[3] == ("LOW", "Clean up unused imports")

    def test_parses_complete_with_low_discovered_tasks(self) -> None:
        output = (
            "<task-status>\n"
            "status: complete\n"
            "\n"
            "[original-plan]\n"
            "- DONE | Implement feature X\n"
            "\n"
            "[discovered]\n"
            "- LOW | Minor formatting cleanup\n"
            "</task-status>"
        )
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is True
        assert len(report.discovered_tasks) == 1
        assert report.discovered_tasks[0] == ("LOW", "Minor formatting cleanup")

    def test_returns_none_when_status_line_missing(self) -> None:
        output = "<task-status>\n" "[original-plan]\n" "- DONE | Something\n" "</task-status>"
        assert parse_task_status_block(output) is None

    def test_handles_extra_whitespace(self) -> None:
        output = "<task-status>  \n" "  status: complete  \n" "  </task-status>"
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is True

    def test_ignores_surrounding_text(self) -> None:
        output = (
            "Here is a bunch of prose about what I did.\n"
            "I made commits and ran tests.\n\n"
            "<task-status>\n"
            "status: complete\n"
            "</task-status>\n"
        )
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is True

    def test_no_discovered_section_omitted(self) -> None:
        output = (
            "<task-status>\n"
            "status: incomplete\n"
            "\n"
            "[original-plan]\n"
            "- INCOMPLETE | Finish the migration script\n"
            "</task-status>"
        )
        report = parse_task_status_block(output)
        assert report is not None
        assert report.status_complete is False
        assert len(report.original_tasks) == 1
        assert report.discovered_tasks == []


# ---------------------------------------------------------------------------
# decide_queue_automation with structured status tests
# ---------------------------------------------------------------------------


class TestDecideQueueAutomationStructured:
    """Tests for structured <task-status> path in decide_queue_automation."""

    @pytest.mark.asyncio
    async def test_uses_structured_status_when_present(self) -> None:
        output = (
            "<task-status>\n"
            "status: incomplete\n"
            "\n"
            "[original-plan]\n"
            "- INCOMPLETE | Remaining work here\n"
            "</task-status>"
        )
        decision = await decide_queue_automation(
            prompt="Do stuff",
            output=output,
            detailed_output="",
            git_tool_events=[],
            judge_runner=None,
        )
        assert decision.should_continue is True
        assert "structured-status" in decision.reason
        assert "incomplete" in decision.reason
        assert decision.task_status is not None

    @pytest.mark.asyncio
    async def test_structured_complete_stops_continuation(self) -> None:
        output = "<task-status>\n" "status: complete\n" "</task-status>"
        decision = await decide_queue_automation(
            prompt="Do stuff",
            output=output,
            detailed_output="",
            git_tool_events=[],
            judge_runner=None,
        )
        assert decision.should_continue is False
        assert "structured-status" in decision.reason
        assert "complete" in decision.reason

    @pytest.mark.asyncio
    async def test_structured_complete_with_critical_discovered_continues(self) -> None:
        output = (
            "<task-status>\n"
            "status: complete\n"
            "\n"
            "[original-plan]\n"
            "- DONE | All original work\n"
            "\n"
            "[discovered]\n"
            "- CRITICAL | Security vulnerability in auth\n"
            "</task-status>"
        )
        decision = await decide_queue_automation(
            prompt="Do stuff",
            output=output,
            detailed_output="",
            git_tool_events=[],
            judge_runner=None,
        )
        assert decision.should_continue is True
        assert "urgent-discovered" in decision.reason

    @pytest.mark.asyncio
    async def test_structured_skips_judge(self) -> None:
        """When structured status is found, judge should not be called."""
        judge_called = False

        async def _judge_runner(_: str) -> str:
            nonlocal judge_called
            judge_called = True
            return '{"remaining_work": true, "confidence": 0.9}'

        output = "<task-status>\n" "status: complete\n" "</task-status>"
        decision = await decide_queue_automation(
            prompt="Do stuff",
            output=output,
            detailed_output="",
            git_tool_events=[],
            judge_runner=_judge_runner,
        )
        assert decision.judge_used is False
        assert not judge_called
        assert decision.should_continue is False

    @pytest.mark.asyncio
    async def test_falls_back_to_heuristics_when_no_block(self) -> None:
        decision = await decide_queue_automation(
            prompt="Do stuff",
            output="Next steps: finish remaining work.",
            detailed_output="",
            git_tool_events=[],
            judge_runner=None,
        )
        assert decision.should_continue is True
        assert "text" in decision.reason
        assert decision.task_status is None


# ---------------------------------------------------------------------------
# build_continue_prompt with task_status tests
# ---------------------------------------------------------------------------


class TestBuildContinuePromptWithStatus:
    """Tests for targeted continue prompt generation."""

    def test_generic_prompt_without_status(self) -> None:
        prompt = build_continue_prompt()
        assert "remaining steps" in prompt.lower()

    def test_generic_prompt_with_none_status(self) -> None:
        prompt = build_continue_prompt(task_status=None)
        assert "remaining steps" in prompt.lower()

    def test_targeted_prompt_with_incomplete_tasks(self) -> None:
        status = TaskStatusReport(
            status_complete=False,
            original_tasks=[
                ("DONE", "Build the API"),
                ("INCOMPLETE", "Add rate limiting"),
                ("INCOMPLETE", "Write integration tests"),
            ],
            discovered_tasks=[],
        )
        prompt = build_continue_prompt(task_status=status)
        assert "Add rate limiting" in prompt
        assert "Write integration tests" in prompt
        assert "Build the API" not in prompt

    def test_targeted_prompt_includes_urgent_discovered(self) -> None:
        status = TaskStatusReport(
            status_complete=False,
            original_tasks=[("DONE", "Build the API")],
            discovered_tasks=[
                ("CRITICAL", "Fix auth vulnerability"),
                ("LOW", "Clean up imports"),
            ],
        )
        prompt = build_continue_prompt(task_status=status)
        assert "Fix auth vulnerability" in prompt
        assert "Clean up imports" not in prompt

    def test_falls_back_to_generic_when_all_done(self) -> None:
        status = TaskStatusReport(
            status_complete=True,
            original_tasks=[("DONE", "Everything")],
            discovered_tasks=[("LOW", "Minor stuff")],
        )
        prompt = build_continue_prompt(task_status=status)
        # No incomplete or urgent discovered → generic prompt
        assert "remaining steps" in prompt.lower()


# ---------------------------------------------------------------------------
# build_task_status_suffix tests
# ---------------------------------------------------------------------------


def test_build_task_status_suffix_contains_key_instructions() -> None:
    suffix = build_task_status_suffix()
    assert "<task-status>" in suffix
    assert "status:" in suffix
    assert "[original-plan]" in suffix
    assert "[discovered]" in suffix
    assert "CRITICAL" in suffix
    assert "DONE" in suffix
    assert "INCOMPLETE" in suffix
