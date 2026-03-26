"""Unit tests for adversarial plan orchestration helpers."""

from types import SimpleNamespace

import pytest

from src.plan_orchestration import orchestrate_adversarial_plan
from src.utils.mode_directives import PlanModeDirective


def _plan(text: str) -> str:
    return (
        "PLAN_STATUS: READY\n"
        "# Implementation Plan\n"
        "## Steps\n"
        f"- {text} 1\n- {text} 2\n- {text} 3\n"
        "## Risks\n- risk\n"
        "## Test Plan\n- unit\n"
    )


@pytest.mark.asyncio
async def test_orchestrate_splan_runs_sequential_revisions() -> None:
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            SimpleNamespace(success=True, output=_plan("planner"), session_id="planner-thread"),
            SimpleNamespace(success=True, output=_plan("reviewer"), session_id="review-thread"),
        ]
    )

    async def _run(model: str, prompt: str, resume_session_id: str | None, persist: bool):
        calls.append((model, prompt))
        return next(responses)

    result = await orchestrate_adversarial_plan(
        prompt="Ship feature X",
        spec=PlanModeDirective(
            strategy="splan",
            models=("gpt-5.4-high", "gpt-5.3-codex"),
        ),
        run_model_turn=_run,
    )

    assert len(calls) == 2
    assert result.planner_model == "gpt-5.4-high"
    assert "reviewer 1" in result.final_plan
    assert "`splan`" in result.summary_markdown


@pytest.mark.asyncio
async def test_orchestrate_fplan_runs_fanout_then_integrates() -> None:
    calls: list[str] = []
    responses = iter(
        [
            SimpleNamespace(success=True, output=_plan("planner"), session_id="planner-thread"),
            SimpleNamespace(success=True, output=_plan("review-a"), session_id="review-a-thread"),
            SimpleNamespace(success=True, output=_plan("review-b"), session_id="review-b-thread"),
            SimpleNamespace(success=True, output=_plan("integrated"), session_id="planner-thread"),
        ]
    )

    async def _run(model: str, prompt: str, resume_session_id: str | None, persist: bool):
        calls.append(model)
        return next(responses)

    result = await orchestrate_adversarial_plan(
        prompt="Ship feature X",
        spec=PlanModeDirective(
            strategy="fplan",
            models=("gpt-5.4-high", "gpt-5.3-codex", "claude-sonnet-4-6-high"),
        ),
        run_model_turn=_run,
    )

    assert calls.count("gpt-5.4-high") == 2
    assert "integrated 1" in result.final_plan
    assert "`fplan`" in result.summary_markdown
