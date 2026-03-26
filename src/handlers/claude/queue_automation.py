"""Queue automation decision helpers for auto-follow queue directives."""

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

_CONTINUE_SIGNAL_PATTERNS = (
    re.compile(r"\bnext\s+steps?\b", re.IGNORECASE),
    re.compile(r"\bremaining\s+steps?\b", re.IGNORECASE),
    re.compile(r"\bif\s+you\s+want[,\s]+the\s+next\s+useful\s+step\b", re.IGNORECASE),
    re.compile(r"\bstill\s+need\s+to\b", re.IGNORECASE),
    re.compile(r"\btodo\b", re.IGNORECASE),
    re.compile(r"\bfollow[-\s]?up\b", re.IGNORECASE),
)
_DONE_SIGNAL_PATTERNS = (
    re.compile(r"\bno\s+more\s+work\b", re.IGNORECASE),
    re.compile(r"\bnothing\s+left\s+to\s+do\b", re.IGNORECASE),
    re.compile(r"\ball\s+(?:done|complete)\b", re.IGNORECASE),
    re.compile(r"\bcompleted\s+everything\b", re.IGNORECASE),
)
_MATH_SIGNAL_PATTERNS = (
    re.compile(r"\bmath(?:ematical)?\b", re.IGNORECASE),
    re.compile(r"\bformula\b", re.IGNORECASE),
    re.compile(r"\bprobab(?:ility|ilities)\b", re.IGNORECASE),
    re.compile(r"\bnumeric(?:al)?\b", re.IGNORECASE),
    re.compile(r"\bstatistic(?:al|s)?\b", re.IGNORECASE),
    re.compile(r"\bmatrix\b", re.IGNORECASE),
    re.compile(r"\bgradient\b", re.IGNORECASE),
)

_CODE_ERROR_CHECK_PROMPT = (
    "Check the latest code changes for code errors and fix any issues you find. "
    "Focus on syntax errors, type errors, runtime exceptions, and broken imports. "
    "Apply fixes directly and summarize what you changed."
)

_LOGIC_ERROR_CHECK_PROMPT = (
    "Check the latest code changes for logic errors and fix any issues you find. "
    "Focus on incorrect conditions, wrong assumptions, edge-case handling, and regressions. "
    "Apply fixes directly and summarize what you changed."
)

_SIMPLIFICATION_CHECK_PROMPT = (
    "Check the latest code changes for simplification opportunities without changing behavior. "
    "Simplify overly complex code paths, remove duplication, and improve clarity. "
    "Apply safe simplifications directly and summarize what you changed."
)

_MATH_ERROR_CHECK_PROMPT = (
    "This appears math-heavy. Check the latest code changes for mathematical mistakes and "
    "numerical issues (sign errors, unit mistakes, off-by-one boundaries, precision pitfalls), "
    "then fix any issues you find and summarize changes."
)

_CONTINUE_PROMPT = (
    "Continue implementing any remaining steps from the previous work. "
    "Do not restate completed work. Execute remaining tasks now, run relevant verification, "
    "and report completed changes succinctly."
)

_JUDGE_PROMPT_TEMPLATE = """
You are a strict queue automation judge.

Task:
Decide whether the previous assistant output indicates there is remaining implementation work.
Also decide whether this appears math-heavy enough to require a math-error check.

Rules:
- Return JSON only (no markdown, no prose).
- JSON schema:
  {{
    "remaining_work": true|false,
    "confidence": 0.0 to 1.0,
    "math_heavy": true|false,
    "reason": "short reason"
  }}
- Prefer false unless there is clear evidence of remaining work.

Prompt context:
{prompt}

Assistant output context:
{output}

Detailed output context:
{detailed_output}
""".strip()

_TASK_STATUS_PROMPT_SUFFIX = """
---

IMPORTANT — TASK STATUS REPORT (mandatory):

At the very end of your response, you MUST include a task status report inside <task-status> tags.

<task-status>
status: incomplete

[original-plan]
- DONE | Description of completed task
- INCOMPLETE | Description of task not yet finished

[discovered]
- CRITICAL | Urgent issue found that blocks correctness
- HIGH | Significant quality or functionality gap
- MEDIUM | Worthwhile improvement discovered
- LOW | Minor polish or cleanup

</task-status>

Rules:
1. "status" must be "complete" or "incomplete".
2. Set status to "complete" ONLY when ALL original plan tasks are DONE and there are no CRITICAL or HIGH discovered tasks.
3. [original-plan] lists every task from the original request. Mark each DONE or INCOMPLETE.
4. [discovered] lists NEW tasks found during work that were NOT in the original request. Rank each: CRITICAL, HIGH, MEDIUM, or LOW. Omit this section if none.
5. When fully complete with no discovered tasks, use the short form:
   <task-status>
   status: complete
   </task-status>
6. Keep descriptions concise (one line, under 120 chars).
7. <task-status> MUST be the last thing in your response.
""".strip()

_TASK_STATUS_BLOCK_RE = re.compile(r"<task-status>\s*(.*?)\s*</task-status>", re.DOTALL)
_TASK_STATUS_LINE_RE = re.compile(r"^-\s*(DONE|INCOMPLETE)\s*\|\s*(.+)$", re.MULTILINE)
_DISCOVERED_TASK_LINE_RE = re.compile(r"^-\s*(CRITICAL|HIGH|MEDIUM|LOW)\s*\|\s*(.+)$", re.MULTILINE)
_STATUS_VALUE_RE = re.compile(r"^status:\s*(complete|incomplete)", re.MULTILINE)


@dataclass(frozen=True)
class TaskStatusReport:
    """Parsed structured task status from Claude's response."""

    status_complete: bool
    original_tasks: list[tuple[str, str]] = field(default_factory=list)
    discovered_tasks: list[tuple[str, str]] = field(default_factory=list)


def parse_task_status_block(output: str) -> Optional[TaskStatusReport]:
    """Extract and parse a <task-status> block from Claude's output.

    Parameters
    ----------
    output : str
        The raw text output from Claude.

    Returns
    -------
    Optional[TaskStatusReport]
        Parsed report, or ``None`` if no valid block was found.
    """
    if not output:
        return None

    block_match = _TASK_STATUS_BLOCK_RE.search(output)
    if not block_match:
        return None

    block_text = block_match.group(1)

    status_match = _STATUS_VALUE_RE.search(block_text)
    if not status_match:
        return None

    status_complete = status_match.group(1) == "complete"

    original_tasks: list[tuple[str, str]] = []
    discovered_tasks: list[tuple[str, str]] = []

    # Split into sections for targeted parsing
    original_section = ""
    discovered_section = ""
    if "[original-plan]" in block_text:
        after_original = block_text.split("[original-plan]", 1)[1]
        if "[discovered]" in after_original:
            original_section = after_original.split("[discovered]", 1)[0]
        else:
            original_section = after_original

    if "[discovered]" in block_text:
        discovered_section = block_text.split("[discovered]", 1)[1]

    for match in _TASK_STATUS_LINE_RE.finditer(original_section):
        original_tasks.append((match.group(1), match.group(2).strip()))

    for match in _DISCOVERED_TASK_LINE_RE.finditer(discovered_section):
        discovered_tasks.append((match.group(1), match.group(2).strip()))

    return TaskStatusReport(
        status_complete=status_complete,
        original_tasks=original_tasks,
        discovered_tasks=discovered_tasks,
    )


def build_task_status_suffix() -> str:
    """Return the prompt suffix instructing Claude to include a task status report."""
    return _TASK_STATUS_PROMPT_SUFFIX


@dataclass(frozen=True)
class QueueAutomationDecision:
    """Decision payload for queue auto-follow behavior."""

    should_continue: bool
    include_math_check: bool
    reason: str
    judge_used: bool
    task_status: Optional[TaskStatusReport] = None


@dataclass(frozen=True)
class _JudgeVerdict:
    """Normalized LLM judge verdict."""

    remaining_work: bool
    confidence: float
    math_heavy: bool
    reason: str


def build_check_prompts(include_math_check: bool) -> list[str]:
    """Return ordered auto-check prompts for the configured policy."""
    prompts = [
        _CODE_ERROR_CHECK_PROMPT,
        _LOGIC_ERROR_CHECK_PROMPT,
        _SIMPLIFICATION_CHECK_PROMPT,
    ]
    if include_math_check:
        prompts.append(_MATH_ERROR_CHECK_PROMPT)
    return prompts


def build_continue_prompt(
    task_status: Optional[TaskStatusReport] = None,
) -> str:
    """Return an auto-continue prompt, optionally targeted to specific remaining tasks.

    Parameters
    ----------
    task_status : Optional[TaskStatusReport]
        If provided, the continue prompt will list the specific incomplete
        original-plan tasks and CRITICAL/HIGH discovered tasks so Claude
        knows exactly what to work on next.
    """
    if task_status is None:
        return _CONTINUE_PROMPT

    incomplete = [desc for status, desc in task_status.original_tasks if status == "INCOMPLETE"]
    urgent_discovered = [
        desc for priority, desc in task_status.discovered_tasks if priority in ("CRITICAL", "HIGH")
    ]

    if not incomplete and not urgent_discovered:
        return _CONTINUE_PROMPT

    parts = [
        "Continue implementing the remaining work. "
        "Do not restate completed work. Execute the following tasks now, "
        "run relevant verification, and report completed changes succinctly.",
        "",
    ]
    if incomplete:
        parts.append("Remaining original-plan tasks:")
        for desc in incomplete:
            parts.append(f"- {desc}")
        parts.append("")
    if urgent_discovered:
        parts.append("Urgent discovered tasks:")
        for desc in urgent_discovered:
            parts.append(f"- {desc}")
        parts.append("")

    return "\n".join(parts).strip()


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return default


def _parse_confidence(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _extract_json_payload(text: str) -> Optional[dict]:
    normalized = (text or "").strip()
    if not normalized:
        return None

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", normalized, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    start = normalized.find("{")
    end = normalized.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = normalized[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    return None


def _heuristic_signals(
    *,
    prompt: str,
    output: str,
    detailed_output: str,
    git_tool_events: list[dict],
) -> tuple[bool, bool, bool, bool]:
    text = "\n\n".join(part for part in [prompt, output, detailed_output] if part)
    strong_continue = any(pattern.search(text) for pattern in _CONTINUE_SIGNAL_PATTERNS)
    strong_done = any(pattern.search(text) for pattern in _DONE_SIGNAL_PATTERNS)
    heuristic_math = any(pattern.search(text) for pattern in _MATH_SIGNAL_PATTERNS)

    commit_signal = False
    for event in git_tool_events:
        command = str(event.get("command") or "")
        mcp_tool = str(event.get("mcp_tool") or "")
        if "git commit" in command or "commit" in mcp_tool.lower():
            commit_signal = True
            break

    return strong_continue, strong_done, heuristic_math, commit_signal


async def _run_llm_judge(
    *,
    judge_runner: Callable[[str], Awaitable[str]],
    prompt: str,
    output: str,
    detailed_output: str,
) -> Optional[_JudgeVerdict]:
    judge_prompt = _JUDGE_PROMPT_TEMPLATE.format(
        prompt=(prompt or "")[:4000],
        output=(output or "")[:8000],
        detailed_output=(detailed_output or "")[:8000],
    )
    raw = await judge_runner(judge_prompt)
    payload = _extract_json_payload(raw)
    if payload is None:
        return None

    return _JudgeVerdict(
        remaining_work=_parse_bool(payload.get("remaining_work"), default=False),
        confidence=_parse_confidence(payload.get("confidence"), default=0.0),
        math_heavy=_parse_bool(payload.get("math_heavy"), default=False),
        reason=str(payload.get("reason") or "").strip(),
    )


async def decide_queue_automation(
    *,
    prompt: str,
    output: str,
    detailed_output: str,
    git_tool_events: list[dict],
    judge_runner: Optional[Callable[[str], Awaitable[str]]] = None,
) -> QueueAutomationDecision:
    """Compute auto-follow decision for a queue item.

    Checks for a structured ``<task-status>`` block first. If found, uses
    that to decide. Otherwise falls back to heuristic + LLM judge logic.
    """
    # --- Structured task-status path (preferred) ---
    combined_output = "\n\n".join(part for part in [output, detailed_output] if part)
    task_status = parse_task_status_block(combined_output)
    if task_status is not None:
        has_urgent_discovered = any(
            priority in ("CRITICAL", "HIGH") for priority, _ in task_status.discovered_tasks
        )
        should_continue = not task_status.status_complete or has_urgent_discovered

        # Still check math heuristic for the math-check pass
        _, _, heuristic_math, _ = _heuristic_signals(
            prompt=prompt,
            output=output,
            detailed_output=detailed_output,
            git_tool_events=git_tool_events,
        )

        reason_parts = ["structured-status"]
        if not task_status.status_complete:
            reason_parts.append("incomplete")
        if has_urgent_discovered:
            reason_parts.append("urgent-discovered")
        if task_status.status_complete and not has_urgent_discovered:
            reason_parts.append("complete")

        return QueueAutomationDecision(
            should_continue=should_continue,
            include_math_check=bool(heuristic_math),
            reason=", ".join(reason_parts),
            judge_used=False,
            task_status=task_status,
        )

    # --- Fallback: heuristic + LLM judge ---
    strong_continue, strong_done, heuristic_math, commit_signal = _heuristic_signals(
        prompt=prompt,
        output=output,
        detailed_output=detailed_output,
        git_tool_events=git_tool_events,
    )

    judge_used = False
    judge_verdict: Optional[_JudgeVerdict] = None
    if judge_runner is not None:
        try:
            judge_verdict = await _run_llm_judge(
                judge_runner=judge_runner,
                prompt=prompt,
                output=output,
                detailed_output=detailed_output,
            )
            judge_used = judge_verdict is not None
        except Exception:
            judge_verdict = None

    llm_continue = False
    llm_reason = ""
    llm_math = None
    if judge_verdict is not None:
        llm_continue = judge_verdict.remaining_work and judge_verdict.confidence >= 0.45
        llm_reason = judge_verdict.reason
        llm_math = judge_verdict.math_heavy

    should_continue = llm_continue or strong_continue or (commit_signal and not strong_done)
    if (
        strong_done
        and not llm_continue
        and not strong_continue
        and not (commit_signal and not strong_done)
    ):
        should_continue = False

    include_math_check = llm_math if llm_math is not None else heuristic_math

    reasons = []
    if llm_continue:
        reasons.append("llm")
    if strong_continue:
        reasons.append("text")
    if commit_signal and not strong_done:
        reasons.append("commit")
    if strong_done:
        reasons.append("done")
    if llm_reason:
        reasons.append(llm_reason)

    return QueueAutomationDecision(
        should_continue=should_continue,
        include_math_check=bool(include_math_check),
        reason=", ".join(reasons) if reasons else "no-continue-signals",
        judge_used=judge_used,
        task_status=None,
    )
