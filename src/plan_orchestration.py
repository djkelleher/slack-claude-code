"""Adversarial plan orchestration helpers."""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.codex.capabilities import is_likely_plan_content
from src.utils.mode_directives import PlanModeDirective


class AdversarialPlanError(RuntimeError):
    """Raised when adversarial plan orchestration cannot complete."""


@dataclass(frozen=True)
class AdversarialPlanResult:
    """Final orchestration output ready for plan approval flow."""

    planner_model: str
    planner_session_id: Optional[str]
    final_plan: str
    summary_markdown: str


_CANONICAL_PLAN_FORMAT_REQUIREMENTS = (
    "Respond with a concrete implementation plan only (no code changes).\n"
    "Use this exact format:\n"
    "PLAN_STATUS: READY\n"
    "# Implementation Plan\n"
    "## Steps\n"
    "## Risks\n"
    "## Test Plan\n"
    "Return only the plan."
)


def _result_field(result: Any, field_name: str, default: Any) -> Any:
    """Read a result field from dataclass/SimpleNamespace-like objects safely."""
    try:
        values = vars(result)
    except TypeError:
        return default
    if field_name in values:
        return values[field_name]
    return default


def _extract_plan_content(text: Optional[str]) -> Optional[str]:
    """Extract/validate canonical plan content with heuristic fallback."""
    if not text:
        return None

    match = re.search(r"(?im)^\s*PLAN_STATUS:\s*READY\s*$", text)
    if match:
        plan_content = text[match.start() :].strip()
        lowered = plan_content.lower()
        required_sections = (
            "# implementation plan",
            "## steps",
            "## risks",
            "## test plan",
        )
        if all(section in lowered for section in required_sections):
            step_count = len(re.findall(r"(?im)^\s*(?:\d+\.\s+|\d+\)\s+|[-*]\s+)", plan_content))
            if step_count >= 3:
                return plan_content

    if is_likely_plan_content(text):
        return text.strip()
    return None


def _initial_planner_prompt(user_prompt: str) -> str:
    return (
        "Create the implementation plan for the task below.\n\n"
        f"Task:\n{user_prompt}\n\n"
        f"{_CANONICAL_PLAN_FORMAT_REQUIREMENTS}"
    )


def _revision_prompt(user_prompt: str, current_plan: str) -> str:
    return (
        "You are revising an implementation plan to improve correctness, sequencing, "
        "risk handling, and validation coverage.\n\n"
        f"Original task:\n{user_prompt}\n\n"
        "Current plan to revise:\n"
        f"{current_plan}\n\n"
        f"{_CANONICAL_PLAN_FORMAT_REQUIREMENTS}"
    )


def _fanout_integration_prompt(
    *,
    user_prompt: str,
    original_plan: str,
    revised_plans: list[tuple[str, str]],
) -> str:
    reviewer_sections = []
    for index, (model, plan) in enumerate(revised_plans, start=1):
        reviewer_sections.append(f"Reviewer {index} ({model}):\n{plan}")
    joined_reviews = "\n\n".join(reviewer_sections)
    return (
        "Integrate the strongest ideas from reviewer revisions into one final plan.\n\n"
        f"Original task:\n{user_prompt}\n\n"
        "Original planner draft:\n"
        f"{original_plan}\n\n"
        "Reviewer revisions:\n"
        f"{joined_reviews}\n\n"
        f"{_CANONICAL_PLAN_FORMAT_REQUIREMENTS}"
    )


def _retry_prompt(base_prompt: str) -> str:
    return (
        f"{base_prompt}\n\n"
        "The previous response did not match the required plan format. "
        "Provide the plan now in the exact required format."
    )


def _build_summary(spec: PlanModeDirective, planner_model: str) -> str:
    reviewers = list(spec.models[1:])
    reviewer_text = ", ".join(f"`{model}`" for model in reviewers) if reviewers else "_none_"
    return (
        "## Adversarial Planning Summary\n"
        f"- Strategy: `{spec.strategy}`\n"
        f"- Planner: `{planner_model}`\n"
        f"- Reviewers: {reviewer_text}"
    )


async def _run_and_extract_plan(
    *,
    model: str,
    prompt: str,
    run_model_turn: Callable[[str, str, Optional[str], bool], Awaitable[Any]],
    resume_session_id: Optional[str],
    persist_session_id: bool,
) -> tuple[Any, str]:
    """Run one model turn and return `(result, extracted_plan)`."""
    result = await run_model_turn(model, prompt, resume_session_id, persist_session_id)
    if not _result_field(result, "success", False):
        error = _result_field(result, "error", None) or "unknown execution failure"
        raise AdversarialPlanError(f"Model `{model}` failed during planning: {error}")

    output = _result_field(result, "output", "") or ""
    extracted = _extract_plan_content(output)
    if extracted:
        return result, extracted

    retry_result = await run_model_turn(
        model,
        _retry_prompt(prompt),
        _result_field(result, "session_id", None),
        persist_session_id,
    )
    if not _result_field(retry_result, "success", False):
        error = _result_field(retry_result, "error", None) or "unknown execution failure"
        raise AdversarialPlanError(f"Model `{model}` failed on plan-format retry: {error}")

    retry_output = _result_field(retry_result, "output", "") or ""
    retry_plan = _extract_plan_content(retry_output)
    if retry_plan:
        return retry_result, retry_plan

    raise AdversarialPlanError(f"Model `{model}` did not produce a detectable implementation plan.")


async def orchestrate_adversarial_plan(
    *,
    prompt: str,
    spec: PlanModeDirective,
    run_model_turn: Callable[[str, str, Optional[str], bool], Awaitable[Any]],
) -> AdversarialPlanResult:
    """Execute `splan`/`fplan` strategy and return final plan for approval."""
    planner_model = spec.models[0]
    reviewer_models = list(spec.models[1:])
    planner_result, planner_plan = await _run_and_extract_plan(
        model=planner_model,
        prompt=_initial_planner_prompt(prompt),
        run_model_turn=run_model_turn,
        resume_session_id=None,
        persist_session_id=True,
    )
    planner_session_id = _result_field(planner_result, "session_id", None)

    final_plan = planner_plan
    if spec.strategy == "splan":
        for reviewer_model in reviewer_models:
            _review_result, revised_plan = await _run_and_extract_plan(
                model=reviewer_model,
                prompt=_revision_prompt(prompt, final_plan),
                run_model_turn=run_model_turn,
                resume_session_id=None,
                persist_session_id=False,
            )
            final_plan = revised_plan
    elif spec.strategy == "fplan":
        review_tasks = [
            _run_and_extract_plan(
                model=reviewer_model,
                prompt=_revision_prompt(prompt, planner_plan),
                run_model_turn=run_model_turn,
                resume_session_id=None,
                persist_session_id=False,
            )
            for reviewer_model in reviewer_models
        ]
        review_results = await asyncio.gather(*review_tasks)
        revised_plans = [
            (reviewer_models[index], extracted_plan)
            for index, (_result, extracted_plan) in enumerate(review_results)
        ]
        integrated_result, integrated_plan = await _run_and_extract_plan(
            model=planner_model,
            prompt=_fanout_integration_prompt(
                user_prompt=prompt,
                original_plan=planner_plan,
                revised_plans=revised_plans,
            ),
            run_model_turn=run_model_turn,
            resume_session_id=planner_session_id,
            persist_session_id=True,
        )
        planner_session_id = _result_field(integrated_result, "session_id", planner_session_id)
        final_plan = integrated_plan
    else:
        raise AdversarialPlanError(f"Unsupported plan strategy: `{spec.strategy}`")

    return AdversarialPlanResult(
        planner_model=planner_model,
        planner_session_id=planner_session_id,
        final_plan=final_plan,
        summary_markdown=_build_summary(spec, planner_model),
    )
