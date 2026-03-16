"""Backend-aware command execution helpers."""

import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger as app_logger

from src.approval.handler import PermissionManager
from src.approval.plan_manager import PlanApprovalManager
from src.codex.approval_bridge import (
    approval_payload_from_decision,
    format_approval_request_for_slack,
)
from src.codex.capabilities import is_likely_plan_content
from src.config import PLANS_DIR, config
from src.database.models import Session
from src.question.manager import QuestionManager
from src.utils.execution_scope import build_session_scope


@dataclass
class CommandRouteResult:
    """Execution result annotated with the selected backend."""

    backend: str
    result: Any


def resolve_backend_for_session(session: Session) -> str:
    """Resolve backend for a session based on selected model."""
    return session.get_backend()


def _result_field(result: Any, field_name: str, default: Any) -> Any:
    """Read a result field from dataclass/SimpleNamespace-like objects safely."""
    try:
        values = vars(result)
    except TypeError:
        return default
    if field_name in values:
        return values[field_name]
    return default


def _normalize_codex_question_input(tool_name: str, tool_input: dict) -> dict:
    """Normalize Codex question tool input into AskUserQuestion-compatible shape."""
    normalized_input = tool_input if isinstance(tool_input, dict) else {}
    if normalized_input.get("questions"):
        return normalized_input

    if normalized_input.get("question"):
        return {
            "questions": [
                {
                    "question": normalized_input.get("question", ""),
                    "header": normalized_input.get("header", "Question"),
                    "options": normalized_input.get("options", []),
                    "multiSelect": normalized_input.get("multiSelect", False),
                }
            ]
        }

    if (tool_name or "").strip().lower() == "request_user_input":
        return {
            "questions": [
                {
                    "question": "Please provide additional input.",
                    "header": "Input Needed",
                    "options": [],
                    "multiSelect": False,
                }
            ]
        }

    return {"questions": []}


def _normalize_claude_question_input(tool_input: dict) -> dict:
    """Normalize Claude AskUserQuestion payload into canonical question shape."""
    normalized_input = tool_input if isinstance(tool_input, dict) else {}
    if normalized_input.get("questions"):
        return normalized_input

    if normalized_input.get("question"):
        return {
            "questions": [
                {
                    "question": normalized_input.get("question", ""),
                    "header": normalized_input.get("header", "Question"),
                    "options": normalized_input.get("options", []),
                    "multiSelect": normalized_input.get("multiSelect", False),
                }
            ]
        }
    return {"questions": []}


def _extract_codex_thread_id(response: dict) -> Optional[str]:
    """Extract a Codex thread ID from RPC response payload."""
    thread = response.get("thread")
    if isinstance(thread, dict):
        thread_id = thread.get("id")
        if thread_id:
            return str(thread_id)

    for key in ("threadId", "id"):
        thread_id = response.get(key)
        if thread_id:
            return str(thread_id)
    return None


def _extract_plan_file_path(plan_text: str) -> Optional[str]:
    """Extract declared plan file path from plan output markers."""
    if not plan_text:
        return None
    plan_override_regex = re.compile(r"(?im)^(?:Plan file|Created Plan):\s*(.+)$")
    matches = plan_override_regex.findall(plan_text)
    if not matches:
        return None
    raw_path = matches[-1].strip().strip("`\"'")
    if not raw_path:
        return None
    return os.path.expanduser(raw_path)


def _build_claude_plan_prompt(
    prompt: str,
    *,
    session_id: Optional[int],
    execution_id: str,
) -> str:
    """Append deterministic plan-file instructions for Claude plan mode."""
    os.makedirs(PLANS_DIR, exist_ok=True)
    if session_id:
        plan_file_name = f"plan-session-{session_id}-{execution_id}.md"
    else:
        plan_file_name = f"plan-{execution_id}.md"
    plan_file_path = os.path.join(PLANS_DIR, plan_file_name)
    return (
        f"{prompt}\n\n"
        f"[Plan mode: Write your plan to this exact file path: {plan_file_path}\n"
        "After writing your plan to that exact file path, include a single line in your response "
        "exactly:\n"
        "Created Plan: <full path to plan file>]"
    )


def _build_codex_plan_prompt(prompt: str) -> str:
    """Append deterministic output requirements for Codex plan mode."""
    return (
        f"{prompt}\n\n"
        "[Plan mode requirements:\n"
        "Respond with a concrete implementation plan only (no code changes).\n"
        "Use this exact format:\n"
        "PLAN_STATUS: READY\n"
        "# Implementation Plan\n"
        "## Steps\n"
        "## Risks\n"
        "## Test Plan\n"
        "If required context is missing, ask clarifying questions first via request_user_input. "
        "After answers are provided, return the plan in this exact format.]"
    )


def _extract_codex_plan_content(text: Optional[str]) -> Optional[str]:
    """Extract/validate Codex plan output produced with deterministic markers."""
    if not text:
        return None

    match = re.search(r"(?im)^\s*PLAN_STATUS:\s*READY\s*$", text)
    if not match:
        return None

    plan_content = text[match.start() :].strip()
    lowered = plan_content.lower()

    required_sections = (
        "# implementation plan",
        "## steps",
        "## risks",
        "## test plan",
    )
    if not all(section in lowered for section in required_sections):
        return None

    step_count = len(re.findall(r"(?im)^\s*(?:\d+\.\s+|\d+\)\s+|[-*]\s+)", plan_content))
    if step_count < 3:
        return None

    return plan_content


def _detect_codex_plan_content(text: Optional[str]) -> tuple[Optional[str], str]:
    """Detect Codex plan content with marker-first, heuristic-fallback strategy."""
    marked_plan = _extract_codex_plan_content(text)
    if marked_plan:
        return marked_plan, "marker"
    if is_likely_plan_content(text):
        return (text or "").strip(), "heuristic"
    return None, "none"


async def _execute_codex_backend(
    *,
    deps: Any,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    session_scope: str,
    execution_id: str,
    on_chunk: Any,
    slack_client: Any,
    user_id: Optional[str],
    logger: Any,
    persist_session_ids: bool,
    auto_answer_questions: bool,
    auto_approve_permissions: bool,
    on_plan_approved: Any,
    on_interaction_resumed: Any,
) -> Any:
    """Execute prompt against Codex backend, including approvals and plan mode."""
    if not deps.codex_executor:
        raise RuntimeError("Codex executor is not configured")

    pending_question = None
    accumulated_context = ""
    question_count = 0
    question_limit_reached = False
    max_questions = config.timeouts.execution.max_questions_per_conversation
    codex_turn_index = 1
    tool_id_namespace = f"turn{codex_turn_index}:"

    async def maybe_swap_on_chunk_after_interaction() -> None:
        nonlocal on_chunk
        if on_interaction_resumed is None:
            return

        replacement_on_chunk = await on_interaction_resumed()
        if replacement_on_chunk is not None:
            on_chunk = replacement_on_chunk

    async def resolve_initial_resume_session_id() -> Optional[str]:
        """Fork inherited channel thread IDs when entering a new Slack thread scope."""
        if not persist_session_ids:
            return session.codex_session_id
        if thread_ts is None or session.codex_session_id is None:
            return session.codex_session_id

        channel_session = await deps.db.get_or_create_session(
            channel_id,
            thread_ts=None,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if channel_session.codex_session_id != session.codex_session_id:
            return session.codex_session_id

        try:
            fork_response = await deps.codex_executor.thread_fork(
                thread_id=session.codex_session_id,
                working_directory=session.working_directory,
            )
        except Exception as e:
            if logger:
                logger.warning(
                    "Failed to fork inherited Codex thread "
                    f"{session.codex_session_id} for scope {session_scope}: {e}"
                )
            return session.codex_session_id

        forked_thread_id = _extract_codex_thread_id(fork_response)
        if not forked_thread_id:
            if logger:
                logger.warning(
                    "Codex thread fork returned no thread ID for scope "
                    f"{session_scope}; continuing with inherited thread "
                    f"{session.codex_session_id}"
                )
            return session.codex_session_id

        if persist_session_ids:
            await deps.db.update_session_codex_id(channel_id, thread_ts, forked_thread_id)
            session.codex_session_id = forked_thread_id
        if logger:
            logger.info(
                f"Forked inherited Codex thread {channel_session.codex_session_id} "
                f"to {forked_thread_id} for scope {session_scope}"
            )
        return forked_thread_id

    async def wrapped_on_chunk(msg: Any) -> None:
        nonlocal accumulated_context
        if msg.tool_activities:
            for tool in msg.tool_activities:
                tool_id = str(tool.id)
                if tool_id and not tool_id.startswith(tool_id_namespace):
                    tool.id = f"{tool_id_namespace}{tool_id}"
        if on_chunk:
            await on_chunk(msg)

        if msg.type == "assistant" and msg.content:
            accumulated_context += msg.content

    async def on_user_input_request(tool_use_id: str, tool_input: dict) -> dict | None:
        nonlocal pending_question, question_count, question_limit_reached
        if question_count >= max_questions:
            question_limit_reached = True
            if logger:
                logger.warning(
                    f"Reached maximum Codex question limit ({max_questions}) "
                    f"for session {session.id}"
                )
            return None

        normalized_tool_input = _normalize_codex_question_input("request_user_input", tool_input)

        if auto_answer_questions:
            questions = QuestionManager.parse_ask_user_question_input(normalized_tool_input)
            auto_answers = QuestionManager.select_recommended_answers(questions)
            question_count += 1
            if logger:
                logger.info(
                    f"Auto-answering Codex question request {tool_use_id} "
                    f"for queue-style execution ({len(questions)} question(s))"
                )
            return QuestionManager.format_answers_for_codex_questions(questions, auto_answers)

        if slack_client is None:
            if pending_question and pending_question.tool_use_id == tool_use_id:
                await QuestionManager.cancel(pending_question.question_id)
                pending_question = None
            return None

        if not pending_question or pending_question.tool_use_id != tool_use_id:
            pending_question = await QuestionManager.create_pending_question(
                session_id=str(session.id),
                channel_id=channel_id,
                thread_ts=thread_ts,
                tool_use_id=tool_use_id,
                tool_input=normalized_tool_input,
            )

        await QuestionManager.post_question_to_slack(
            pending_question,
            slack_client,
            deps.db,
            context_text=accumulated_context,
        )
        question_count += 1
        answers = await QuestionManager.wait_for_answer(pending_question.question_id)
        if not answers:
            pending_question = None
            return None

        response_payload = QuestionManager.format_answer_for_codex_request(pending_question)
        pending_question = None
        await maybe_swap_on_chunk_after_interaction()
        return response_payload

    async def on_approval_request(method: str, approval_input: dict) -> dict | None:
        if auto_approve_permissions:
            if logger:
                logger.info(
                    f"Auto-approving Codex permission request {method} " "for queue-style execution"
                )
            return approval_payload_from_decision(method, True)

        if slack_client is None:
            return None

        tool_name, tool_input = format_approval_request_for_slack(method, approval_input)
        approved = await PermissionManager.request_approval(
            session_id=str(session.id),
            channel_id=channel_id,
            tool_name=tool_name,
            tool_input=tool_input,
            user_id=user_id,
            thread_ts=thread_ts,
            slack_client=slack_client,
            db=deps.db,
            auto_approve_tools=config.AUTO_APPROVE_TOOLS,
        )
        await maybe_swap_on_chunk_after_interaction()
        return approval_payload_from_decision(method, approved)

    async def run_codex_turn(turn_prompt: str, resume_session_id: Optional[str]) -> Any:
        result = await deps.codex_executor.execute(
            prompt=turn_prompt,
            working_directory=session.working_directory,
            session_id=session_scope,
            resume_session_id=resume_session_id,
            execution_id=execution_id,
            on_chunk=wrapped_on_chunk,
            on_user_input_request=on_user_input_request,
            on_approval_request=on_approval_request,
            permission_mode=session.permission_mode,
            sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
            db_session_id=session.id,
            model=session.model,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if result.session_id and persist_session_ids:
            await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)
        return result

    initial_resume_session_id = await resolve_initial_resume_session_id()
    first_prompt = prompt
    if session.permission_mode == "plan":
        first_prompt = _build_codex_plan_prompt(prompt)

    result = await run_codex_turn(first_prompt, initial_resume_session_id)

    if question_limit_reached:
        result.output = (
            (result.output or accumulated_context)
            + f"\n\n_Reached maximum question limit ({max_questions})._"
        ).strip()
        result.success = False
    if pending_question:
        await QuestionManager.cancel(pending_question.question_id)
        pending_question = None

    if session.permission_mode == "plan" and result.success and slack_client is not None:
        plan_content, plan_detection_source = _detect_codex_plan_content(result.output)

        if not plan_content and result.session_id:
            retry_log = "Codex plan response not detected; requesting canonical plan-format retry"
            app_logger.info(retry_log)
            if logger:
                logger.info(retry_log)
            codex_turn_index += 1
            tool_id_namespace = f"turn{codex_turn_index}:"
            retry_prompt = (
                "You are still in plan mode. Provide the implementation plan now in this exact "
                "format:\n"
                "PLAN_STATUS: READY\n"
                "# Implementation Plan\n"
                "## Steps\n"
                "## Risks\n"
                "## Test Plan\n"
                "Return only the plan."
            )
            result = await run_codex_turn(retry_prompt, result.session_id)
            if result.success:
                plan_content, plan_detection_source = _detect_codex_plan_content(result.output)

        if plan_content and result.success:
            approval_log = (
                f"Codex plan response ready (source={plan_detection_source}); "
                "requesting user approval"
            )
            app_logger.info(approval_log)
            if logger:
                logger.info(approval_log)
            approved = await PlanApprovalManager.request_approval(
                session_id=str(session.id),
                channel_id=channel_id,
                plan_content=plan_content,
                claude_session_id=result.session_id or "",
                prompt=prompt,
                user_id=user_id,
                thread_ts=thread_ts,
                slack_client=slack_client,
                plan_file_path=None,
            )

            if approved:
                codex_turn_index += 1
                tool_id_namespace = f"turn{codex_turn_index}:"
                await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
                session.permission_mode = config.DEFAULT_BYPASS_MODE
                if on_plan_approved:
                    replacement_on_chunk = await on_plan_approved()
                    if replacement_on_chunk is not None:
                        on_chunk = replacement_on_chunk

                result = await run_codex_turn(
                    "Plan approved. Please proceed with the implementation.",
                    result.session_id,
                )
            else:
                result.success = False
                result.output = "_Plan not approved. Staying in plan mode until you provide feedback._"
        else:
            skipped_log = (
                "Codex plan mode response did not produce a detectable plan after retry; "
                "skipping approval prompt"
            )
            app_logger.warning(skipped_log)
            if logger:
                logger.warning(skipped_log)

    return result


async def _execute_claude_backend(
    *,
    deps: Any,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    session_scope: str,
    execution_id: str,
    on_chunk: Any,
    slack_client: Any,
    user_id: Optional[str],
    logger: Any,
    persist_session_ids: bool,
    auto_answer_questions: bool,
    auto_approve_permissions: bool,
    on_plan_approved: Any,
    on_interaction_resumed: Any,
) -> Any:
    """Execute prompt against Claude backend, including questions and plan approval."""
    pending_question = None
    accumulated_context = ""
    question_count = 0
    max_questions = config.timeouts.execution.max_questions_per_conversation

    async def maybe_swap_on_chunk_after_interaction() -> None:
        nonlocal on_chunk
        if on_interaction_resumed is None:
            return

        replacement_on_chunk = await on_interaction_resumed()
        if replacement_on_chunk is not None:
            on_chunk = replacement_on_chunk

    async def wrapped_on_chunk(msg: Any) -> None:
        nonlocal accumulated_context, pending_question
        if msg.tool_activities and (slack_client is not None or auto_answer_questions):
            for tool in msg.tool_activities:
                if tool.name != "AskUserQuestion":
                    continue
                if tool.result is not None:
                    continue
                if pending_question and pending_question.tool_use_id == tool.id:
                    continue
                pending_question = await QuestionManager.create_pending_question(
                    session_id=str(session.id),
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    tool_use_id=tool.id,
                    tool_input=_normalize_claude_question_input(tool.input),
                )
        if on_chunk:
            await on_chunk(msg)
        if msg.type == "assistant" and msg.content:
            accumulated_context += msg.content

    async def run_claude_turn(
        turn_prompt: str,
        resume_session_id: Optional[str],
        *,
        mode: Optional[str],
        turn_execution_id: str,
    ) -> Any:
        result = await deps.executor.execute(
            prompt=turn_prompt,
            working_directory=session.working_directory,
            session_id=session_scope,
            resume_session_id=resume_session_id,
            execution_id=turn_execution_id,
            on_chunk=wrapped_on_chunk,
            permission_mode=mode,
            db_session_id=session.id,
            model=session.model,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if result.session_id and persist_session_ids:
            await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)
        return result

    first_prompt = prompt
    if session.permission_mode == "plan":
        first_prompt = _build_claude_plan_prompt(
            prompt,
            session_id=session.id,
            execution_id=execution_id,
        )
    result = await run_claude_turn(
        first_prompt,
        session.claude_session_id,
        mode=session.permission_mode,
        turn_execution_id=execution_id,
    )

    while (
        _result_field(result, "has_pending_question", False)
        and pending_question
        and result.session_id
        and question_count < max_questions
    ):
        if slack_client is None and not auto_answer_questions:
            break
        question_count += 1
        if auto_answer_questions:
            pending_question.answers = QuestionManager.select_recommended_answers(
                pending_question.questions
            )
            answer_text = QuestionManager.format_answer_for_claude(pending_question).strip()
            if not answer_text:
                answer_text = "Use your recommended/default option and continue."
            if logger:
                logger.info(
                    f"Auto-answering Claude question {pending_question.tool_use_id} "
                    f"for queue-style execution ({len(pending_question.questions)} question(s))"
                )
        else:
            await QuestionManager.post_question_to_slack(
                pending_question,
                slack_client,
                deps.db,
                context_text=accumulated_context,
            )
            answers = await QuestionManager.wait_for_answer(pending_question.question_id)
            if not answers:
                pending_question = None
                result.output = (accumulated_context + "\n\n_Question was cancelled._").strip()
                result.success = False
                break
            answer_text = QuestionManager.format_answer_for_claude(pending_question)
            await maybe_swap_on_chunk_after_interaction()
        pending_question = None
        result = await run_claude_turn(
            answer_text,
            result.session_id,
            mode=session.permission_mode,
            turn_execution_id=f"{execution_id}-q{question_count}",
        )

    if question_count >= max_questions and pending_question:
        result.output = (
            (accumulated_context or result.output)
            + f"\n\n_Reached maximum question limit ({max_questions}). Please start a new conversation._"
        ).strip()
        result.success = False

    if pending_question:
        await QuestionManager.cancel(pending_question.question_id)
        pending_question = None

    if _result_field(result, "has_pending_plan_approval", False) and slack_client is not None:
        plan_text = _result_field(result, "plan_subagent_result", "") or result.output or ""
        plan_file_path = _extract_plan_file_path(plan_text) or _extract_plan_file_path(
            result.output or ""
        )
        plan_content = plan_text.strip()
        if plan_file_path and os.path.isfile(plan_file_path):
            try:
                with open(plan_file_path, "r", encoding="utf-8") as f:
                    plan_content = f.read().strip()
            except Exception as e:
                if logger:
                    logger.warning(f"Failed to read plan file {plan_file_path}: {e}")

        if not plan_content:
            plan_content = "⚠️ No plan content was produced. Ask the assistant to generate a concrete plan and try again."

        approved = await PlanApprovalManager.request_approval(
            session_id=str(session.id),
            channel_id=channel_id,
            plan_content=plan_content,
            claude_session_id=result.session_id or "",
            prompt=prompt,
            user_id=user_id,
            thread_ts=thread_ts,
            slack_client=slack_client,
            plan_file_path=plan_file_path,
        )
        if approved:
            await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
            session.permission_mode = config.DEFAULT_BYPASS_MODE
            if on_plan_approved:
                replacement_on_chunk = await on_plan_approved()
                if replacement_on_chunk is not None:
                    on_chunk = replacement_on_chunk
            result = await run_claude_turn(
                "Plan approved. Please proceed with the implementation.",
                result.session_id,
                mode=config.DEFAULT_BYPASS_MODE,
                turn_execution_id=f"{execution_id}-plan-{uuid.uuid4().hex[:8]}",
            )
        else:
            result.success = False
            result.output = "_Plan not approved. Staying in plan mode until you provide feedback._"

    return result


async def execute_for_session(
    deps: Any,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    execution_id: str,
    on_chunk: Any = None,
    slack_client: Any = None,
    user_id: Optional[str] = None,
    logger: Any = None,
    persist_session_ids: bool = True,
    auto_answer_questions: bool = False,
    auto_approve_permissions: bool = False,
    session_scope_override: Optional[str] = None,
    on_plan_approved: Any = None,
    on_interaction_resumed: Any = None,
) -> CommandRouteResult:
    """Execute a prompt with the correct backend and persist resumed session IDs."""
    backend = resolve_backend_for_session(session)
    session_scope = session_scope_override or build_session_scope(channel_id, thread_ts)

    if backend == "codex":
        result = await _execute_codex_backend(
            deps=deps,
            session=session,
            prompt=prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            on_chunk=on_chunk,
            slack_client=slack_client,
            user_id=user_id,
            logger=logger,
            persist_session_ids=persist_session_ids,
            auto_answer_questions=auto_answer_questions,
            auto_approve_permissions=auto_approve_permissions,
            on_plan_approved=on_plan_approved,
            on_interaction_resumed=on_interaction_resumed,
        )
        return CommandRouteResult(backend=backend, result=result)

    result = await _execute_claude_backend(
        deps=deps,
        session=session,
        prompt=prompt,
        channel_id=channel_id,
        thread_ts=thread_ts,
        session_scope=session_scope,
        execution_id=execution_id,
        on_chunk=on_chunk,
        slack_client=slack_client,
        user_id=user_id,
        logger=logger,
        persist_session_ids=persist_session_ids,
        auto_answer_questions=auto_answer_questions,
        auto_approve_permissions=auto_approve_permissions,
        on_plan_approved=on_plan_approved,
        on_interaction_resumed=on_interaction_resumed,
    )
    return CommandRouteResult(backend=backend, result=result)
