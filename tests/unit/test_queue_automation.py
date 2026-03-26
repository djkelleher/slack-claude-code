"""Unit tests for queue auto-follow decision helpers."""

import pytest

from src.handlers.claude.queue_automation import (
    build_check_prompts,
    build_continue_prompt,
    decide_queue_automation,
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
