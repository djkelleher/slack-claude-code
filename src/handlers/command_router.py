"""Backend-aware command execution helpers."""

import os
import re
import uuid
from dataclasses import dataclass, replace
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
from src.database.models import Session, WorkspaceLease
from src.git.service import GitService
from src.git.workspace_manager import (
    PreparedWorkspace,
    WorkspaceLeaseError,
    WorkspaceManager,
)
from src.question.manager import QuestionManager
from src.utils.execution_scope import build_session_scope

_QUEUE_PAUSE_ON_QUESTION_SIGNAL = "__QUEUE_PAUSE_ON_QUESTION__"


@dataclass
class CommandRouteResult:
    """Execution result annotated with the selected backend."""

    backend: str
    result: Any


@dataclass
class _ConversationState:
    """Shared mutable state for one backend conversation loop."""

    accumulated_context: str = ""
    pending_question: Any = None
    question_count: int = 0
    question_limit_reached: bool = False


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
    try:
        os.makedirs(PLANS_DIR, exist_ok=True)
    except OSError:
        pass
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


async def _maybe_swap_on_chunk_after_interaction(
    on_interaction_resumed: Any,
    on_chunk: Any,
) -> Any:
    """Swap streaming target after a Slack interaction resumes execution."""
    if on_interaction_resumed is None:
        return on_chunk
    replacement_on_chunk = await on_interaction_resumed()
    if replacement_on_chunk is None:
        return on_chunk
    return replacement_on_chunk


def _append_assistant_context(state: _ConversationState, msg: Any) -> None:
    """Track assistant text for Slack question context and fallback output."""
    if msg.type == "assistant" and msg.content:
        state.accumulated_context += msg.content


def _append_workspace_notes(result: Any, notes: list[str]) -> Any:
    """Append workspace lifecycle notes to the result output and detailed output."""
    filtered_notes = [note.strip() for note in notes if note and note.strip()]
    if not filtered_notes:
        return result

    note_block = "\n".join(f"- {note}" for note in filtered_notes)
    existing_output = (result.output or "").strip()
    if existing_output:
        result.output = f"{existing_output}\n\nWorkspace Notes\n{note_block}"
    else:
        result.output = f"Workspace Notes\n{note_block}"

    detailed_output = (result.detailed_output or "").strip()
    if detailed_output:
        result.detailed_output = f"{detailed_output}\n\nWorkspace Notes\n{note_block}"
    return result


def _build_auto_worktree_finalize_prompt(lease: WorkspaceLease) -> str:
    """Build a follow-up prompt that converts dirty workspace changes into one commit."""
    target_branch = lease.target_branch or "the target branch"
    return (
        "You are finalizing changes from an isolated auto worktree.\n"
        f"- Auto worktree branch: `{lease.worktree_name}`\n"
        f"- Target branch for reintegration: `{target_branch}`\n\n"
        "Review the current git status. If the changes should be kept, stage only the intended "
        "files and create one commit with a concise message. If there is nothing worth keeping, "
        "clean the working tree and explain why. Run the most relevant tests you can identify "
        "before committing, and summarize what you verified."
    )


def _build_auto_worktree_conflict_prompt(lease: WorkspaceLease, conflict_files: list[str]) -> str:
    """Build a follow-up prompt for resolving merge conflicts in the target worktree."""
    conflict_summary = "\n".join(f"- {path}" for path in conflict_files[:20]) or "- (unknown)"
    return (
        "A raw git merge reported conflicts while reintegrating an auto worktree.\n"
        f"- Source branch: `{lease.worktree_name}`\n"
        f"- Target branch: `{lease.target_branch or 'unknown'}`\n"
        "- Resolve all unmerged files in this target worktree.\n"
        "- Preserve the intended behavior from both sides of the merge.\n"
        "- Run the most relevant tests you can identify.\n"
        "- Stage the resolutions and complete the merge commit.\n\n"
        "Current unmerged files:\n"
        f"{conflict_summary}"
    )


def _build_untracked_prepared_workspace(
    *,
    session: Session,
    channel_id: str,
    thread_ts: Optional[str],
    session_scope: str,
    execution_id: str,
) -> PreparedWorkspace:
    """Build a direct-workspace fallback when tests provide a lightweight DB stub."""
    return PreparedWorkspace(
        lease=WorkspaceLease(
            session_id=session.id or 0,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            leased_root=session.working_directory,
            leased_cwd=session.working_directory,
            base_cwd=session.working_directory,
            lease_kind="direct",
            status="active",
        ),
        session=session,
        persist_session_ids=True,
    )


async def _post_or_auto_answer_question(
    *,
    backend: str,
    pending_question: Any,
    slack_client: Any,
    deps: Any,
    context_text: str,
    auto_answer_questions: bool,
    logger: Any,
    log_prefix: str,
) -> str | dict | None:
    """Resolve a pending question through auto-answering or Slack UI."""
    if auto_answer_questions:
        pending_question.answers = QuestionManager.select_recommended_answers(
            pending_question.questions
        )
        response = QuestionManager.format_answer(pending_question, backend=backend)
        if backend == "claude" and isinstance(response, str):
            response = response.strip() or "Use your recommended/default option and continue."
        if logger:
            logger.info(
                f"Auto-answering {log_prefix} question {pending_question.tool_use_id} "
                f"for queue-style execution ({len(pending_question.questions)} question(s))"
            )
        await QuestionManager.cancel(pending_question.question_id)
        return response

    if slack_client is None:
        return None

    await QuestionManager.post_question_to_slack(
        pending_question,
        slack_client,
        deps.db,
        context_text=context_text,
    )
    answers = await QuestionManager.wait_for_answer(pending_question.question_id)
    if not answers:
        return None
    return QuestionManager.format_answer(pending_question, backend=backend)


async def _request_plan_approval(
    *,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    slack_client: Any,
    user_id: Optional[str],
    plan_content: str,
    resume_session_id: str,
    plan_file_path: Optional[str],
) -> bool:
    """Open shared plan approval flow for either backend."""
    return await PlanApprovalManager.request_approval(
        session_id=str(session.id),
        channel_id=channel_id,
        plan_content=plan_content,
        resume_session_id=resume_session_id,
        prompt=prompt,
        user_id=user_id,
        thread_ts=thread_ts,
        slack_client=slack_client,
        plan_file_path=plan_file_path,
    )


async def _run_workspace_follow_up(
    *,
    backend: str,
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
    auto_answer_questions: bool,
    auto_approve_permissions: bool,
    pause_on_questions: bool,
) -> Any:
    """Run a non-persistent follow-up turn inside an already selected workspace."""
    follow_up_session = replace(session)
    if backend == "codex":
        follow_up_session.permission_mode = "default"
        return await _execute_codex_backend(
            deps=deps,
            session=follow_up_session,
            prompt=prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            on_chunk=on_chunk,
            slack_client=slack_client,
            user_id=user_id,
            logger=logger,
            persist_session_ids=False,
            auto_answer_questions=auto_answer_questions,
            auto_approve_permissions=auto_approve_permissions,
            pause_on_questions=pause_on_questions,
            on_plan_approved=None,
            on_interaction_resumed=None,
        )

    follow_up_session.permission_mode = config.DEFAULT_BYPASS_MODE
    return await _execute_claude_backend(
        deps=deps,
        session=follow_up_session,
        prompt=prompt,
        channel_id=channel_id,
        thread_ts=thread_ts,
        session_scope=session_scope,
        execution_id=execution_id,
        on_chunk=on_chunk,
        slack_client=slack_client,
        user_id=user_id,
        logger=logger,
        persist_session_ids=False,
        auto_answer_questions=auto_answer_questions,
        auto_approve_permissions=auto_approve_permissions,
        pause_on_questions=pause_on_questions,
        on_plan_approved=None,
        on_interaction_resumed=None,
    )


async def _reintegrate_auto_worktree(
    *,
    deps: Any,
    backend: str,
    prepared_workspace: PreparedWorkspace,
    result: Any,
    channel_id: str,
    thread_ts: Optional[str],
    session_scope: str,
    execution_id: str,
    on_chunk: Any,
    slack_client: Any,
    user_id: Optional[str],
    logger: Any,
    auto_answer_questions: bool,
    auto_approve_permissions: bool,
    pause_on_questions: bool,
) -> tuple[Any, str, list[str]]:
    """Finalize and, when safe, reintegrate an auto worktree back into its target branch."""
    lease = prepared_workspace.lease
    workspace_manager = WorkspaceManager(
        db=deps.db,
        claude_executor=deps.executor,
        codex_executor=deps.codex_executor,
        git_service=GitService(),
    )
    notes = [f"Execution used auto worktree `{lease.leased_cwd}`."]
    git_service = workspace_manager.git_service

    try:
        worktree_status = await git_service.get_status(lease.leased_root)
    except Exception as exc:
        notes.append(
            f"Auto worktree `{lease.leased_root}` was kept because its status could not be read: {exc}"
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    if not worktree_status.has_changes():
        try:
            cleanup_note = await workspace_manager.cleanup_auto_worktree(lease)
        except Exception as exc:
            notes.append(
                f"Auto worktree `{lease.leased_root}` was clean, but cleanup failed: {exc}"
            )
            return (
                _append_workspace_notes(result, notes),
                "needs_manual_attention",
                notes,
            )
        if cleanup_note:
            notes.append(cleanup_note)
        return _append_workspace_notes(result, notes), "clean-noop", notes

    if not result.success:
        notes.append(
            f"Auto worktree `{lease.leased_root}` was kept because the execution failed with local changes."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    finalize_result = await _run_workspace_follow_up(
        backend=backend,
        deps=deps,
        session=replace(
            prepared_workspace.session,
            working_directory=lease.leased_cwd,
            claude_session_id=result.session_id if backend == "claude" else None,
            codex_session_id=result.session_id if backend == "codex" else None,
        ),
        prompt=_build_auto_worktree_finalize_prompt(lease),
        channel_id=channel_id,
        thread_ts=thread_ts,
        session_scope=f"{session_scope}:finalize:{execution_id}",
        execution_id=f"{execution_id}-finalize",
        on_chunk=on_chunk,
        slack_client=slack_client,
        user_id=user_id,
        logger=logger,
        auto_answer_questions=auto_answer_questions,
        auto_approve_permissions=auto_approve_permissions,
        pause_on_questions=pause_on_questions,
    )
    if finalize_result.output:
        result.output = "\n\n".join(
            part for part in [result.output, "Finalize Output", finalize_result.output] if part
        )
    if not finalize_result.success:
        notes.append(f"Auto worktree `{lease.leased_root}` was kept because finalization failed.")
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    post_finalize_status = await git_service.get_status(lease.leased_root)
    if post_finalize_status.has_changes():
        notes.append(
            f"Auto worktree `{lease.leased_root}` was kept because finalization did not leave a clean tree."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    if not lease.target_worktree_path or not lease.worktree_name:
        notes.append(
            f"Auto worktree `{lease.leased_root}` was kept because merge metadata is incomplete."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    active_target_lease = await deps.db.get_active_workspace_lease_by_root(
        lease.target_worktree_path
    )
    if active_target_lease and active_target_lease.execution_id != lease.execution_id:
        notes.append(
            f"Auto worktree `{lease.leased_root}` is ready but target `{lease.target_worktree_path}` "
            "is still leased by another execution."
        )
        return _append_workspace_notes(result, notes), "target_busy", notes

    target_status = await git_service.get_status(lease.target_worktree_path)
    if target_status.has_changes():
        notes.append(
            f"Auto worktree `{lease.leased_root}` was kept because target `{lease.target_worktree_path}` is dirty."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    merge_success, merge_message = await git_service.merge_branch(
        lease.target_worktree_path,
        lease.worktree_name,
    )
    if merge_success:
        notes.append(f"Merged `{lease.worktree_name}` into `{lease.target_branch or 'target'}`.")
        if merge_message:
            notes.append(merge_message)
        try:
            cleanup_note = await workspace_manager.cleanup_auto_worktree(lease)
        except Exception as exc:
            notes.append(
                f"Merge succeeded, but auto worktree cleanup failed for `{lease.leased_root}`: {exc}"
            )
            return (
                _append_workspace_notes(result, notes),
                "needs_manual_attention",
                notes,
            )
        if cleanup_note:
            notes.append(cleanup_note)
        return _append_workspace_notes(result, notes), "merged", notes

    conflict_files = await workspace_manager.get_unmerged_files(lease.target_worktree_path)
    conflict_result = await _run_workspace_follow_up(
        backend=backend,
        deps=deps,
        session=replace(
            prepared_workspace.session,
            working_directory=lease.target_worktree_path,
            claude_session_id=None,
            codex_session_id=None,
        ),
        prompt=_build_auto_worktree_conflict_prompt(lease, conflict_files),
        channel_id=channel_id,
        thread_ts=thread_ts,
        session_scope=f"{session_scope}:merge:{execution_id}",
        execution_id=f"{execution_id}-merge",
        on_chunk=on_chunk,
        slack_client=slack_client,
        user_id=user_id,
        logger=logger,
        auto_answer_questions=auto_answer_questions,
        auto_approve_permissions=auto_approve_permissions,
        pause_on_questions=pause_on_questions,
    )
    if conflict_result.output:
        result.output = "\n\n".join(
            part for part in [result.output, "Merge Resolve Output", conflict_result.output] if part
        )
    if not conflict_result.success:
        notes.append(
            f"Merge conflicts in `{lease.target_worktree_path}` were not resolved automatically."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    remaining_conflicts = await workspace_manager.get_unmerged_files(lease.target_worktree_path)
    if remaining_conflicts:
        notes.append(
            f"Merge conflicts remain in target `{lease.target_worktree_path}`; kept auto worktree `{lease.leased_root}`."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    resolved_target_status = await git_service.get_status(lease.target_worktree_path)
    if resolved_target_status.has_changes():
        notes.append(
            f"Target `{lease.target_worktree_path}` still has unstaged or uncommitted changes after merge resolution."
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes

    notes.append(
        f"Merged `{lease.worktree_name}` into `{lease.target_branch or 'target'}` after conflict resolution."
    )
    try:
        cleanup_note = await workspace_manager.cleanup_auto_worktree(lease)
    except Exception as exc:
        notes.append(
            f"Merge succeeded, but auto worktree cleanup failed for `{lease.leased_root}`: {exc}"
        )
        return _append_workspace_notes(result, notes), "needs_manual_attention", notes
    if cleanup_note:
        notes.append(cleanup_note)
    return _append_workspace_notes(result, notes), "merged", notes


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
    pause_on_questions: bool,
    on_plan_approved: Any,
    on_interaction_resumed: Any,
) -> Any:
    """Execute prompt against Codex backend, including approvals and plan mode."""
    if not deps.codex_executor:
        raise RuntimeError("Codex executor is not configured")

    state = _ConversationState()
    max_questions = config.timeouts.execution.max_questions_per_conversation
    question_pause_requested = False
    codex_turn_index = 1
    tool_id_namespace = f"turn{codex_turn_index}:"

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
        if msg.tool_activities:
            for tool in msg.tool_activities:
                tool_id = str(tool.id)
                if tool_id and not tool_id.startswith(tool_id_namespace):
                    tool.id = f"{tool_id_namespace}{tool_id}"
        if on_chunk:
            await on_chunk(msg)
        _append_assistant_context(state, msg)

    async def on_user_input_request(tool_use_id: str, tool_input: dict) -> dict | None:
        nonlocal on_chunk
        nonlocal question_pause_requested
        if state.question_count >= max_questions:
            state.question_limit_reached = True
            if logger:
                logger.warning(
                    f"Reached maximum Codex question limit ({max_questions}) "
                    f"for session {session.id}"
                )
            return None

        normalized_tool_input = QuestionManager.normalize_question_tool_input(
            tool_input,
            default_question="Please provide additional input.",
            default_header="Input Needed",
        )
        questions = QuestionManager.parse_ask_user_question_input(normalized_tool_input)

        if pause_on_questions:
            deferred_answers = await QuestionManager.consume_deferred_answer(
                session_id=str(session.id),
                channel_id=channel_id,
                thread_ts=thread_ts,
                questions=questions,
            )
            if deferred_answers is not None:
                state.question_count += 1
                if logger:
                    logger.info(
                        f"Replaying deferred answer for Codex question request {tool_use_id} "
                        f"({len(questions)} question(s))"
                    )
                response = QuestionManager.serialize_answers(
                    questions,
                    deferred_answers,
                    backend="codex",
                )
                if isinstance(response, dict):
                    return response

            if slack_client is not None:
                if not state.pending_question or state.pending_question.tool_use_id != tool_use_id:
                    state.pending_question = await QuestionManager.create_pending_question(
                        session_id=str(session.id),
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        tool_use_id=tool_use_id,
                        tool_input=normalized_tool_input,
                        defer_for_resume=True,
                    )
                await QuestionManager.post_question_to_slack(
                    state.pending_question,
                    slack_client,
                    deps.db,
                    context_text=state.accumulated_context,
                )
            question_pause_requested = True
            raise RuntimeError(_QUEUE_PAUSE_ON_QUESTION_SIGNAL)

        if auto_answer_questions:
            auto_answers = QuestionManager.select_recommended_answers(questions)
            state.question_count += 1
            if logger:
                logger.info(
                    f"Auto-answering Codex question request {tool_use_id} "
                    f"for queue-style execution ({len(questions)} question(s))"
                )
            response = QuestionManager.serialize_answers(
                questions,
                auto_answers,
                backend="codex",
            )
            if not isinstance(response, dict):
                return None
            return response

        if slack_client is None:
            if state.pending_question and state.pending_question.tool_use_id == tool_use_id:
                await QuestionManager.cancel(state.pending_question.question_id)
                state.pending_question = None
            return None

        if not state.pending_question or state.pending_question.tool_use_id != tool_use_id:
            state.pending_question = await QuestionManager.create_pending_question(
                session_id=str(session.id),
                channel_id=channel_id,
                thread_ts=thread_ts,
                tool_use_id=tool_use_id,
                tool_input=normalized_tool_input,
            )

        state.question_count += 1
        response_payload = await _post_or_auto_answer_question(
            backend="codex",
            pending_question=state.pending_question,
            slack_client=slack_client,
            deps=deps,
            context_text=state.accumulated_context,
            auto_answer_questions=False,
            logger=logger,
            log_prefix="Codex",
        )
        if not isinstance(response_payload, dict):
            state.pending_question = None
            return None

        state.pending_question = None
        on_chunk = await _maybe_swap_on_chunk_after_interaction(on_interaction_resumed, on_chunk)
        return response_payload

    async def on_approval_request(method: str, approval_input: dict) -> dict | None:
        nonlocal on_chunk
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
        on_chunk = await _maybe_swap_on_chunk_after_interaction(on_interaction_resumed, on_chunk)
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

    if (
        question_pause_requested
        or _result_field(result, "error", "") == _QUEUE_PAUSE_ON_QUESTION_SIGNAL
    ):
        result.success = False
        result.error = _QUEUE_PAUSE_ON_QUESTION_SIGNAL
        result.paused_on_question = True

    if state.question_limit_reached:
        result.output = (
            (result.output or state.accumulated_context)
            + f"\n\n_Reached maximum question limit ({max_questions})._"
        ).strip()
        result.success = False
    if state.pending_question:
        if not question_pause_requested:
            await QuestionManager.cancel(state.pending_question.question_id)
        state.pending_question = None

    if question_pause_requested:
        return result

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
            approved = await _request_plan_approval(
                session=session,
                prompt=prompt,
                channel_id=channel_id,
                thread_ts=thread_ts,
                slack_client=slack_client,
                user_id=user_id,
                plan_content=plan_content,
                resume_session_id=result.session_id or "",
                plan_file_path=None,
            )

            if approved:
                codex_turn_index += 1
                tool_id_namespace = f"turn{codex_turn_index}:"
                await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
                session.permission_mode = config.DEFAULT_BYPASS_MODE
                if on_plan_approved:
                    on_chunk = await _maybe_swap_on_chunk_after_interaction(
                        on_plan_approved,
                        on_chunk,
                    )

                result = await run_codex_turn(
                    "Plan approved. Please proceed with the implementation.",
                    result.session_id,
                )
            else:
                result.success = False
                result.output = (
                    "_Plan not approved. Staying in plan mode until you provide feedback._"
                )
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
    pause_on_questions: bool,
    on_plan_approved: Any,
    on_interaction_resumed: Any,
    allow_live_pty: bool,
) -> Any:
    """Execute prompt against Claude backend, including questions and plan approval."""
    state = _ConversationState()
    max_questions = config.timeouts.execution.max_questions_per_conversation
    question_pause_requested = False

    async def wrapped_on_chunk(msg: Any) -> None:
        if msg.tool_activities and (
            slack_client is not None or auto_answer_questions or pause_on_questions
        ):
            for tool in msg.tool_activities:
                if tool.name != "AskUserQuestion":
                    continue
                if tool.result is not None:
                    continue
                if state.pending_question and state.pending_question.tool_use_id == tool.id:
                    continue
                state.pending_question = await QuestionManager.create_pending_question(
                    session_id=str(session.id),
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    tool_use_id=tool.id,
                    tool_input=QuestionManager.normalize_question_tool_input(tool.input),
                    defer_for_resume=pause_on_questions,
                )
        if on_chunk:
            await on_chunk(msg)
        _append_assistant_context(state, msg)

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
            allow_live_pty=allow_live_pty,
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
        and state.pending_question
        and result.session_id
        and state.question_count < max_questions
    ):
        if pause_on_questions:
            deferred_answers = await QuestionManager.consume_deferred_answer(
                session_id=str(session.id),
                channel_id=channel_id,
                thread_ts=thread_ts,
                questions=state.pending_question.questions,
            )
            if deferred_answers is not None:
                state.pending_question.answers = deferred_answers
                answer_text = QuestionManager.format_answer(
                    state.pending_question, backend="claude"
                )
                if isinstance(answer_text, str):
                    state.question_count += 1
                    if logger:
                        logger.info(
                            "Replaying deferred answer for Claude question "
                            f"{state.pending_question.tool_use_id} "
                            f"({len(state.pending_question.questions)} question(s))"
                        )
                    state.pending_question = None
                    result = await run_claude_turn(
                        answer_text,
                        result.session_id,
                        mode=session.permission_mode,
                        turn_execution_id=f"{execution_id}-q{state.question_count}",
                    )
                    continue
            question_pause_requested = True
            if slack_client is not None:
                await QuestionManager.post_question_to_slack(
                    state.pending_question,
                    slack_client,
                    deps.db,
                    context_text=state.accumulated_context,
                )
            break
        if slack_client is None and not auto_answer_questions:
            break
        state.question_count += 1
        answer_text = await _post_or_auto_answer_question(
            backend="claude",
            pending_question=state.pending_question,
            slack_client=slack_client,
            deps=deps,
            context_text=state.accumulated_context,
            auto_answer_questions=auto_answer_questions,
            logger=logger,
            log_prefix="Claude",
        )
        if not isinstance(answer_text, str):
            state.pending_question = None
            result.output = (state.accumulated_context + "\n\n_Question was cancelled._").strip()
            result.success = False
            break
        if not auto_answer_questions:
            on_chunk = await _maybe_swap_on_chunk_after_interaction(
                on_interaction_resumed, on_chunk
            )
        state.pending_question = None
        result = await run_claude_turn(
            answer_text,
            result.session_id,
            mode=session.permission_mode,
            turn_execution_id=f"{execution_id}-q{state.question_count}",
        )

    if (
        state.question_count >= max_questions
        and state.pending_question
        and not question_pause_requested
    ):
        result.output = (
            (state.accumulated_context or result.output)
            + f"\n\n_Reached maximum question limit ({max_questions}). Please start a new conversation._"
        ).strip()
        result.success = False

    if state.pending_question:
        if not question_pause_requested:
            await QuestionManager.cancel(state.pending_question.question_id)
        state.pending_question = None

    if question_pause_requested:
        result.success = False
        result.error = _QUEUE_PAUSE_ON_QUESTION_SIGNAL
        result.paused_on_question = True
        return result

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

        approved = await _request_plan_approval(
            session=session,
            prompt=prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            slack_client=slack_client,
            user_id=user_id,
            plan_content=plan_content,
            resume_session_id=result.session_id or "",
            plan_file_path=plan_file_path,
        )
        if approved:
            await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
            session.permission_mode = config.DEFAULT_BYPASS_MODE
            if on_plan_approved:
                on_chunk = await _maybe_swap_on_chunk_after_interaction(
                    on_plan_approved,
                    on_chunk,
                )
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
    pause_on_questions: bool = False,
    session_scope_override: Optional[str] = None,
    on_plan_approved: Any = None,
    on_interaction_resumed: Any = None,
    allow_live_pty: bool = False,
) -> CommandRouteResult:
    """Execute a prompt with the correct backend and persist resumed session IDs."""
    backend = resolve_backend_for_session(session)
    session_scope = session_scope_override or build_session_scope(channel_id, thread_ts)
    workspace_manager = WorkspaceManager(
        db=deps.db,
        claude_executor=deps.executor,
        codex_executor=deps.codex_executor,
        git_service=GitService(),
    )
    prepared_workspace: Optional[PreparedWorkspace] = None
    result: Any = None
    release_status = "released"
    merge_status: Optional[str] = None
    lease_tracking_enabled = True

    try:
        prepared_workspace = await workspace_manager.prepare_workspace(
            session=session,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
        )
    except AttributeError:
        prepared_workspace = _build_untracked_prepared_workspace(
            session=session,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
        )
        lease_tracking_enabled = False
    except WorkspaceLeaseError:
        raise

    effective_session = prepared_workspace.session
    effective_persist_session_ids = persist_session_ids and prepared_workspace.persist_session_ids

    try:
        if backend == "codex":
            result = await _execute_codex_backend(
                deps=deps,
                session=effective_session,
                prompt=prompt,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                on_chunk=on_chunk,
                slack_client=slack_client,
                user_id=user_id,
                logger=logger,
                persist_session_ids=effective_persist_session_ids,
                auto_answer_questions=auto_answer_questions,
                auto_approve_permissions=auto_approve_permissions,
                pause_on_questions=pause_on_questions,
                on_plan_approved=on_plan_approved,
                on_interaction_resumed=on_interaction_resumed,
            )
        else:
            result = await _execute_claude_backend(
                deps=deps,
                session=effective_session,
                prompt=prompt,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                on_chunk=on_chunk,
                slack_client=slack_client,
                user_id=user_id,
                logger=logger,
                persist_session_ids=effective_persist_session_ids,
                auto_answer_questions=auto_answer_questions,
                auto_approve_permissions=auto_approve_permissions,
                pause_on_questions=pause_on_questions,
                on_plan_approved=on_plan_approved,
                on_interaction_resumed=on_interaction_resumed,
                allow_live_pty=allow_live_pty,
            )

        if prepared_workspace.uses_auto_worktree:
            result, merge_status, _notes = await _reintegrate_auto_worktree(
                deps=deps,
                backend=backend,
                prepared_workspace=prepared_workspace,
                result=result,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                on_chunk=on_chunk,
                slack_client=slack_client,
                user_id=user_id,
                logger=logger,
                auto_answer_questions=auto_answer_questions,
                auto_approve_permissions=auto_approve_permissions,
                pause_on_questions=pause_on_questions,
            )
            if merge_status in {"merged", "clean-noop"}:
                release_status = "merged"
            elif merge_status == "target_busy":
                release_status = "released"
            else:
                release_status = "needs_manual_attention"
        return CommandRouteResult(backend=backend, result=result)
    finally:
        if prepared_workspace is not None and lease_tracking_enabled:
            final_status = release_status
            if result is not None and not prepared_workspace.uses_auto_worktree:
                final_status = "released"
            if prepared_workspace.uses_auto_worktree and merge_status is None:
                final_status = (
                    "needs_manual_attention" if result is None or not result.success else "released"
                )
            await workspace_manager.release_workspace(
                execution_id,
                status=final_status,
                merge_status=merge_status,
            )
