"""Backend-aware command execution helpers."""

from dataclasses import dataclass
from typing import Any, Optional

from src.approval.plan_manager import PlanApprovalManager
from src.claude.streaming import _concat_with_spacing
from src.codex.capabilities import apply_codex_mode_to_prompt
from src.config import config
from src.database.models import Session
from src.question.manager import QuestionManager


@dataclass
class CommandRouteResult:
    """Execution result annotated with the selected backend."""

    backend: str
    result: Any


def resolve_backend_for_session(session: Session) -> str:
    """Resolve backend for a session based on selected model."""
    return session.get_backend()


def _is_codex_question_tool(tool_name: str) -> bool:
    """Return True when a Codex tool invocation requests user input."""
    normalized = (tool_name or "").strip().lower()
    return normalized in {"askuserquestion", "ask_user_question", "request_user_input"}


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
) -> CommandRouteResult:
    """Execute a prompt with the correct backend and persist resumed session IDs."""
    backend = resolve_backend_for_session(session)

    if backend == "codex":
        if not deps.codex_executor:
            raise RuntimeError("Codex executor is not configured")

        pending_question = None
        accumulated_context = ""

        async def wrapped_on_chunk(msg: Any) -> None:
            nonlocal pending_question, accumulated_context
            if on_chunk:
                await on_chunk(msg)

            if msg.type == "assistant" and msg.content:
                accumulated_context = _concat_with_spacing(accumulated_context, msg.content)

            if not msg.tool_activities:
                return

            for tool in msg.tool_activities:
                if _is_codex_question_tool(tool.name) and tool.result is None:
                    if not pending_question or pending_question.tool_use_id != tool.id:
                        pending_question = await QuestionManager.create_pending_question(
                            session_id=str(session.id),
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            tool_use_id=tool.id,
                            tool_input=_normalize_codex_question_input(tool.name, tool.input),
                        )

        async def on_user_input_request(tool_use_id: str, tool_input: dict) -> dict | None:
            nonlocal pending_question
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
                    tool_input=_normalize_codex_question_input("request_user_input", tool_input),
                )

            await QuestionManager.post_question_to_slack(
                pending_question,
                slack_client,
                deps.db,
                context_text=accumulated_context,
            )
            answers = await QuestionManager.wait_for_answer(pending_question.question_id)
            if not answers:
                pending_question = None
                return None

            response_payload = QuestionManager.format_answer_for_codex_request(pending_question)
            pending_question = None
            return response_payload

        execution_prompt = apply_codex_mode_to_prompt(prompt, session.permission_mode)
        result = await deps.codex_executor.execute(
            prompt=execution_prompt,
            working_directory=session.working_directory,
            session_id=channel_id,
            resume_session_id=session.codex_session_id,
            execution_id=execution_id,
            on_chunk=wrapped_on_chunk,
            on_user_input_request=on_user_input_request,
            sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
            permission_mode=session.permission_mode,
            db_session_id=session.id,
            model=session.model,
            channel_id=channel_id,
        )

        if result.session_id:
            await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)

        question_count = 0
        max_questions = config.timeouts.execution.max_questions_per_conversation
        while pending_question and result.session_id and question_count < max_questions:
            if slack_client is None:
                await QuestionManager.cancel(pending_question.question_id)
                pending_question = None
                break

            question_count += 1
            await QuestionManager.post_question_to_slack(
                pending_question,
                slack_client,
                deps.db,
                context_text=accumulated_context,
            )
            answers = await QuestionManager.wait_for_answer(pending_question.question_id)
            if not answers:
                result.output = (
                    result.output or accumulated_context
                ) + "\n\n_Question was cancelled._"
                result.success = False
                pending_question = None
                break

            answer_text = QuestionManager.format_answer_for_claude(pending_question)
            pending_question = None
            result = await deps.codex_executor.execute(
                prompt=answer_text,
                working_directory=session.working_directory,
                session_id=channel_id,
                resume_session_id=result.session_id,
                execution_id=execution_id,
                on_chunk=wrapped_on_chunk,
                on_user_input_request=on_user_input_request,
                sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
                approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
                permission_mode=session.permission_mode,
                db_session_id=session.id,
                model=session.model,
                channel_id=channel_id,
            )
            if result.session_id:
                await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)

        if question_count >= max_questions and pending_question:
            result.output = (
                (result.output or accumulated_context)
                + f"\n\n_Reached maximum question limit ({max_questions}). Please start a new conversation._"
            )
            result.success = False
            await QuestionManager.cancel(pending_question.question_id)
            pending_question = None

        result_output = getattr(result, "output", "")
        if (
            session.permission_mode == "plan"
            and result.success
            and result_output
            and slack_client is not None
        ):
            if logger:
                logger.info("Codex plan response ready, requesting user approval")
            approved = await PlanApprovalManager.request_approval(
                session_id=str(session.id),
                channel_id=channel_id,
                plan_content=result_output,
                claude_session_id=result.session_id or "",
                prompt=prompt,
                user_id=user_id,
                thread_ts=thread_ts,
                slack_client=slack_client,
                plan_file_path=None,
            )

            if approved:
                await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
                session.permission_mode = config.DEFAULT_BYPASS_MODE

                result = await deps.codex_executor.execute(
                    prompt="Plan approved. Please proceed with the implementation.",
                    working_directory=session.working_directory,
                    session_id=channel_id,
                    resume_session_id=result.session_id,
                    execution_id=execution_id,
                    on_chunk=wrapped_on_chunk,
                    on_user_input_request=on_user_input_request,
                    sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
                    approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
                    permission_mode=session.permission_mode,
                    db_session_id=session.id,
                    model=session.model,
                    channel_id=channel_id,
                )
                if result.session_id:
                    await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)
            else:
                result.success = False
                result.output = (
                    (getattr(result, "output", "") or "")
                    + "\n\n_Plan not approved. Staying in plan mode until you provide feedback._"
                ).strip()

        return CommandRouteResult(backend=backend, result=result)

    result = await deps.executor.execute(
        prompt=prompt,
        working_directory=session.working_directory,
        session_id=channel_id,
        resume_session_id=session.claude_session_id,
        execution_id=execution_id,
        on_chunk=on_chunk,
        permission_mode=session.permission_mode,
        db_session_id=session.id,
        model=session.model,
        channel_id=channel_id,
    )

    if result.session_id:
        await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

    return CommandRouteResult(backend=backend, result=result)
