"""Queue command handlers: /q, /qc, /qv, /qclear, /qdelete, and /qr."""

import asyncio
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from loguru import logger
from slack_bolt.async_app import AsyncApp

from src.config import config
from src.database.models import Session
from src.git.service import GitService
from src.tasks.manager import TaskManager
from src.tasks.queue_plan import (
    QueuePlanError,
    contains_queue_plan_markers,
    materialize_queue_plan_text,
    parse_queue_plan_submission,
)
from src.utils.execution_scope import build_session_scope
from src.utils.formatters.base import escape_markdown
from src.utils.formatters.command import error_message
from src.utils.formatters.queue import (
    queue_item_running,
    queue_scope_overview,
    queue_status,
)
from src.utils.formatters.streaming import processing_message
from src.utils.streaming import StreamingMessageState, create_streaming_callback

from ..base import CommandContext, HandlerDependencies, slack_command
from ..command_router import execute_for_session
from ..execution_runtime import streaming_flags_for_session

# Default timeout for queue processors (1 hour)
QUEUE_PROCESSOR_TIMEOUT = 3600
_QUEUE_START_LOCKS: dict[str, asyncio.Lock] = {}
_QUEUE_START_LOCKS_GUARD: Optional[asyncio.Lock] = None
_QUEUE_START_LOCKS_LOOP: Optional[asyncio.AbstractEventLoop] = None
_PARALLEL_HISTORY_COMMAND_LIMIT = 10
_PARALLEL_HISTORY_OUTPUT_LIMIT = 1000
_PARALLEL_HISTORY_TOTAL_LIMIT = 12000
_THREAD_TS_PATTERN = re.compile(r"^\d+\.\d+$")


@dataclass(frozen=True)
class _ParallelExecutionConfig:
    group_id: str
    claude_preamble: str
    codex_base_thread_id: Optional[str]


def _queue_task_id(channel_id: str, thread_ts: Optional[str]) -> str:
    """Build a stable task id for a queue processor scoped to channel/thread."""
    return f"queue_{build_session_scope(channel_id, thread_ts)}"


async def _create_queue_task(
    coro,
    channel_id: str,
    thread_ts: Optional[str],
    task_logger=None,
) -> asyncio.Task:
    """Create a queue processor task with proper tracking.

    Uses TaskManager for lifecycle management with automatic cleanup.
    """
    task = asyncio.create_task(coro)
    task_id = _queue_task_id(channel_id, thread_ts)

    await TaskManager.register(
        task_id=task_id,
        task=task,
        channel_id=channel_id,
        task_type="queue_processor",
        timeout_seconds=QUEUE_PROCESSOR_TIMEOUT,
    )

    def done_callback(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc:
                log = task_logger or logger
                log.error(f"Queue processor failed: {exc}", exc_info=exc)

    task.add_done_callback(done_callback)
    return task


async def _is_queue_processor_running(channel_id: str, thread_ts: Optional[str]) -> bool:
    """Check if a queue processor is already running for a scope."""
    task_id = _queue_task_id(channel_id, thread_ts)
    tracked = await TaskManager.get(task_id)
    return tracked is not None and not tracked.is_done


async def _get_queue_start_lock(task_id: str) -> asyncio.Lock:
    """Return a per-scope lock used to serialize queue processor startup."""
    global _QUEUE_START_LOCKS_GUARD, _QUEUE_START_LOCKS_LOOP
    current_loop = asyncio.get_running_loop()
    if _QUEUE_START_LOCKS_LOOP is not current_loop:
        _QUEUE_START_LOCKS.clear()
        _QUEUE_START_LOCKS_LOOP = current_loop
        _QUEUE_START_LOCKS_GUARD = asyncio.Lock()

    if _QUEUE_START_LOCKS_GUARD is None:
        _QUEUE_START_LOCKS_GUARD = asyncio.Lock()

    async with _QUEUE_START_LOCKS_GUARD:
        if task_id not in _QUEUE_START_LOCKS:
            _QUEUE_START_LOCKS[task_id] = asyncio.Lock()
        return _QUEUE_START_LOCKS[task_id]


async def _cleanup_queue_start_lock(task_id: str) -> None:
    """Remove idle startup lock for a scope to avoid unbounded lock-map growth."""
    if _QUEUE_START_LOCKS_GUARD is None:
        _QUEUE_START_LOCKS.pop(task_id, None)
        return
    async with _QUEUE_START_LOCKS_GUARD:
        lock = _QUEUE_START_LOCKS.get(task_id)
        if lock and not lock.locked():
            _QUEUE_START_LOCKS.pop(task_id, None)


def _prompt_preview(prompt: str, limit: int = 180) -> str:
    """Return a compact, single-line prompt preview for status text."""
    flattened = " ".join(prompt.split())
    if len(flattened) <= limit:
        return flattened
    return f"{flattened[:limit]}..."


def _queue_processing_log_line(sequence_number: int, prompt: str) -> str:
    """Build queue processing log text for Slack + logger output."""
    return f"Processing queue item {sequence_number}: {_prompt_preview(prompt)}"


def _parallel_processing_log_line(item_id: int, group_id: str, prompt: str) -> str:
    """Build queue processing log text for parallel queue items."""
    return f"Processing parallel queue item #{item_id} ({group_id}): {_prompt_preview(prompt)}"


def _build_queue_completion_text(status_counts: dict[str, int]) -> str:
    """Build a Slack-friendly queue completion summary."""
    total = sum(status_counts.values())
    parts = []
    if status_counts.get("completed"):
        parts.append(f"{status_counts['completed']} completed")
    if status_counts.get("failed"):
        parts.append(f"{status_counts['failed']} failed")
    if status_counts.get("cancelled"):
        parts.append(f"{status_counts['cancelled']} cancelled")
    detail = ", ".join(parts) if parts else "no items processed"
    return f"Queue finished: processed {total} item(s) ({detail})."


def _build_queue_halted_text(
    state: str, status_counts: dict[str, int], remaining_count: int
) -> str:
    """Build a queue summary when processing stops before draining the queue."""
    total = sum(status_counts.values())
    parts = []
    if status_counts.get("completed"):
        parts.append(f"{status_counts['completed']} completed")
    if status_counts.get("failed"):
        parts.append(f"{status_counts['failed']} failed")
    if status_counts.get("cancelled"):
        parts.append(f"{status_counts['cancelled']} cancelled")
    detail = ", ".join(parts) if parts else "no items processed"
    verb = "paused" if state == "paused" else "stopped"
    return (
        f"Queue {verb}: processed {total} item(s) ({detail}). "
        f"{remaining_count} item(s) remain queued."
    )


def _queue_state_notice(state: str) -> str:
    """Return a short operator-facing notice for a non-running queue."""
    if state == "paused":
        return "Queue is paused. Use `/qc resume` to continue."
    if state == "stopped":
        return "Queue is stopped. Use `/qc resume` to continue."
    return ""


async def _queue_state_for_submission(
    deps: HandlerDependencies,
    channel_id: str,
    thread_ts: Optional[str],
    replace_pending: bool,
) -> str:
    """Return effective queue state for a new submission.

    Replacing pending items starts a new queue generation, so any prior
    pause/stop control should not block the replacement queue from running.
    """
    queue_state = await _get_queue_state(deps, channel_id, thread_ts)
    if replace_pending and queue_state != "running":
        queue_state = (
            await deps.db.update_queue_control_state(channel_id, thread_ts, "running")
        ).state
    return queue_state


def _queue_scope_label(thread_ts: Optional[str]) -> str:
    """Return a human-friendly queue scope label."""
    if thread_ts:
        return f"Thread {thread_ts}"
    return "Channel queue"


def _parse_scope_selector(selector: str) -> Optional[str]:
    """Parse an optional queue scope selector."""
    normalized = selector.strip()
    if normalized.lower() == "channel":
        return None
    if _THREAD_TS_PATTERN.match(normalized):
        return normalized
    raise ValueError(
        "Scope must be `channel` or a Slack thread timestamp like `1234567890.123456`."
    )


async def _get_queue_state(
    deps: HandlerDependencies, channel_id: str, thread_ts: Optional[str]
) -> str:
    """Return the persisted queue execution state for a scope."""
    control = await deps.db.get_queue_control(channel_id, thread_ts)
    return control.state


async def _recover_stale_running_items(
    *,
    channel_id: str,
    thread_ts: Optional[str],
    deps: HandlerDependencies,
    log,
) -> int:
    """Mark stale DB-running queue items cancelled when no processor task is active."""
    if await _is_queue_processor_running(channel_id, thread_ts):
        return 0

    running_items = await deps.db.get_running_queue_items(channel_id, thread_ts)
    if not running_items:
        return 0

    recovered = 0
    for running_item in running_items:
        updated = await deps.db.update_queue_item_status(
            running_item.id,
            "cancelled",
            error_message="Recovered stale running queue item (no active queue processor).",
        )
        if updated:
            recovered += 1

    if recovered:
        scope = build_session_scope(channel_id, thread_ts)
        log.warning(f"Recovered {recovered} stale running queue item(s) for scope {scope}")
    return recovered


def _extract_codex_thread_id(response: dict) -> Optional[str]:
    """Extract a thread id from a Codex thread/fork response."""
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


async def _build_claude_parallel_preamble(deps: HandlerDependencies, session: Session) -> str:
    """Build a bounded lossy Claude context preamble for parallel queue items."""
    history, _ = await deps.db.get_command_history(
        session.id, limit=_PARALLEL_HISTORY_COMMAND_LIMIT
    )
    if not history:
        return ""

    sections: list[str] = []
    remaining = _PARALLEL_HISTORY_TOTAL_LIMIT
    for entry in reversed(history):
        output = (entry.output or entry.error_message or "").strip()
        if len(output) > _PARALLEL_HISTORY_OUTPUT_LIMIT:
            output = output[:_PARALLEL_HISTORY_OUTPUT_LIMIT] + "..."
        section = (
            f"Prompt: {entry.command.strip()}\n"
            f"Status: {entry.status}\n"
            f"Output:\n{output or '(no output)'}"
        )
        if len(section) > remaining:
            section = section[:remaining]
        if section:
            sections.append(section)
            remaining -= len(section)
        if remaining <= 0:
            break

    if not sections:
        return ""

    return "Recent session context (lossy local history approximation):\n\n" + "\n\n".join(sections)


def _build_parallel_prompt(prompt: str, claude_preamble: str) -> str:
    """Compose the final prompt for a Claude parallel queue item."""
    if not claude_preamble:
        return prompt
    return f"{claude_preamble}\n\n" "Current queued prompt:\n" f"{prompt}"


async def _execute_queue_item(
    item,
    *,
    channel_id: str,
    thread_ts: Optional[str],
    scope: str,
    deps: HandlerDependencies,
    client,
    log,
    base_session: Session,
    sequence_label: str,
    override_resume_ids: dict[str, dict[str, str]],
    parallel_config: Optional[_ParallelExecutionConfig] = None,
) -> Optional[str]:
    """Execute a single queue item with shared Slack/result handling."""
    claimed = await deps.db.update_queue_item_status(item.id, "running")
    if not claimed:
        log.info(f"Queue item #{item.id} no longer pending in scope {scope}, skipping")
        return None

    processing_log_line = (
        _parallel_processing_log_line(item.id, parallel_config.group_id, item.prompt)
        if parallel_config
        else _queue_processing_log_line(int(sequence_label), item.prompt)
    )
    smart_concat = True
    terminal_style = False
    if isinstance(base_session, Session):
        smart_concat, terminal_style = streaming_flags_for_session(base_session)
    log.info(f"{processing_log_line} (scope={scope}, queue_item_id={item.id})")

    message_ts = None
    streaming_state = None
    try:

        def _create_streaming_state(message_timestamp: str) -> StreamingMessageState:
            state = StreamingMessageState(
                channel_id=channel_id,
                message_ts=message_timestamp,
                prompt=processing_log_line,
                client=client,
                logger=log,
                track_tools=True,
                smart_concat=smart_concat,
                terminal_style=terminal_style,
                truncate_output=False,
            )
            state.start_heartbeat()
            return state

        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=processing_log_line,
            blocks=queue_item_running(item, sequence_label),
        )
        message_ts = response["ts"]
        streaming_state = _create_streaming_state(message_ts)
        on_chunk = create_streaming_callback(streaming_state)

        async def on_plan_approved():
            nonlocal message_ts, streaming_state
            if streaming_state is not None:
                await streaming_state.finalize()

            exec_response = await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"{processing_log_line} (implementing approved plan)",
                blocks=processing_message(
                    ":white_check_mark: *Plan approved!* Executing implementation..."
                ),
            )
            message_ts = exec_response["ts"]
            streaming_state = _create_streaming_state(message_ts)
            return create_streaming_callback(streaming_state)

        effective_session = base_session
        effective_prompt = item.prompt
        persist_session_ids = True
        session_scope_override = None
        override_key: Optional[str] = None

        if parallel_config:
            persist_session_ids = False
            working_directory = item.working_directory_override or base_session.working_directory
            session_scope_override = f"{scope}:parallel:{parallel_config.group_id}:{item.id}"
            if effective_session.get_backend() == "codex":
                codex_thread_id = None
                if parallel_config.codex_base_thread_id and deps.codex_executor:
                    fork_response = await deps.codex_executor.thread_fork(
                        thread_id=parallel_config.codex_base_thread_id,
                        working_directory=working_directory,
                    )
                    codex_thread_id = _extract_codex_thread_id(fork_response)
                effective_session = replace(
                    base_session,
                    working_directory=working_directory,
                    claude_session_id=None,
                    codex_session_id=codex_thread_id,
                )
            else:
                effective_prompt = _build_parallel_prompt(
                    item.prompt, parallel_config.claude_preamble
                )
                effective_session = replace(
                    base_session,
                    working_directory=working_directory,
                    claude_session_id=None,
                    codex_session_id=None,
                )
        elif item.working_directory_override:
            override_key = str(Path(item.working_directory_override).expanduser())
            resume_state = override_resume_ids.get(override_key, {})
            effective_session = replace(
                base_session,
                working_directory=item.working_directory_override,
                claude_session_id=resume_state.get("claude"),
                codex_session_id=resume_state.get("codex"),
            )
            persist_session_ids = False

        route = await execute_for_session(
            deps=deps,
            session=effective_session,
            prompt=effective_prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            execution_id=f"queue_{item.id}",
            on_chunk=on_chunk,
            slack_client=client,
            logger=log,
            persist_session_ids=persist_session_ids,
            auto_answer_questions=config.QUEUE_AUTO_ANSWER_QUESTIONS,
            session_scope_override=session_scope_override,
            on_plan_approved=on_plan_approved,
        )
        result = route.result
        if override_key and result.session_id:
            backend_resume = override_resume_ids.setdefault(override_key, {})
            backend_resume[route.backend] = result.session_id

        if result.success:
            await deps.db.update_queue_item_status(item.id, "completed", output=result.output)
            final_status = "completed"
        else:
            await deps.db.update_queue_item_status(
                item.id,
                "failed",
                output=result.output,
                error_message=result.error,
            )
            final_status = "failed"
        final_output = result.output or result.error or "No output"
        if streaming_state and not streaming_state.accumulated_output.strip() and final_output:
            streaming_state.accumulated_output = final_output
        if streaming_state:
            await streaming_state.finalize(is_error=not result.success)
        return final_status

    except asyncio.CancelledError:
        await deps.db.update_queue_item_status(
            item.id,
            "cancelled",
            error_message="Queue processor cancelled",
        )
        if streaming_state:
            if not streaming_state.accumulated_output.strip():
                streaming_state.accumulated_output = "Queue item cancelled while processing."
            await streaming_state.finalize(is_error=True)
        elif message_ts:
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Queue item #{item.id} cancelled",
                blocks=error_message("Queue item cancelled while processing."),
            )
        raise
    except Exception as e:
        log.error(f"Queue item {item.id} failed in scope {scope}: {e}")
        await deps.db.update_queue_item_status(item.id, "failed", error_message=str(e))
        if streaming_state:
            if not streaming_state.accumulated_output.strip():
                streaming_state.accumulated_output = f"Queue item failed: {e}"
            await streaming_state.finalize(is_error=True)
        else:
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"Queue item #{item.id} failed",
                    blocks=error_message(f"Queue item failed: {e}"),
                )
            except Exception as notify_error:
                log.error(
                    f"Failed to send failure notification for queue item {item.id} in "
                    f"scope {scope}: {notify_error}"
                )
        return "failed"
    finally:
        if streaming_state:
            await streaming_state.stop_heartbeat()


async def _run_parallel_group(
    *,
    channel_id: str,
    thread_ts: Optional[str],
    scope: str,
    deps: HandlerDependencies,
    client,
    log,
    session: Session,
    items: list,
) -> list[str]:
    """Execute a queue parallel group with bounded concurrency."""
    if not items:
        return []

    group_id = items[0].parallel_group_id or "parallel"
    group_limit = items[0].parallel_limit or len(items)
    concurrency = min(group_limit, len(items))
    parallel_config = _ParallelExecutionConfig(
        group_id=group_id,
        claude_preamble=(
            await _build_claude_parallel_preamble(deps, session)
            if session.get_backend() == "claude"
            else ""
        ),
        codex_base_thread_id=session.codex_session_id if session.get_backend() == "codex" else None,
    )

    pending_items = list(items)
    active_tasks: dict[asyncio.Task, int] = {}
    statuses: list[str] = []

    def start_task(queue_item) -> None:
        task = asyncio.create_task(
            _execute_queue_item(
                queue_item,
                channel_id=channel_id,
                thread_ts=thread_ts,
                scope=scope,
                deps=deps,
                client=client,
                log=log,
                base_session=session,
                sequence_label=f"{queue_item.id} · parallel {group_id}",
                override_resume_ids={},
                parallel_config=parallel_config,
            )
        )
        active_tasks[task] = queue_item.id

    try:
        while pending_items and len(active_tasks) < concurrency:
            start_task(pending_items.pop(0))

        while active_tasks:
            done, _ = await asyncio.wait(active_tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            queue_state = await _get_queue_state(deps, channel_id, thread_ts)
            for task in done:
                active_tasks.pop(task, None)
                status = await task
                if status:
                    statuses.append(status)
                if pending_items and queue_state == "running":
                    start_task(pending_items.pop(0))
        return statuses
    except asyncio.CancelledError:
        for task in active_tasks:
            task.cancel()
        for task in list(active_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise


async def ensure_queue_processor(
    channel_id: str,
    thread_ts: Optional[str],
    deps: HandlerDependencies,
    client,
    task_logger=None,
) -> None:
    """Ensure the queue processor is active for this channel/thread scope."""
    log = task_logger or logger
    scope = build_session_scope(channel_id, thread_ts)
    task_id = _queue_task_id(channel_id, thread_ts)
    start_lock = await _get_queue_start_lock(task_id)
    async with start_lock:
        if await _is_queue_processor_running(channel_id, thread_ts):
            log.info(f"Queue processor already active for scope {scope}")
            return
        log.info(f"Starting queue processor for scope {scope}")
        await _create_queue_task(
            _process_queue(channel_id, deps, client, task_logger, thread_ts=thread_ts),
            channel_id,
            thread_ts,
            task_logger,
        )


def register_queue_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register queue command handlers."""
    git_service = GitService()

    @app.command("/q")
    @slack_command(require_text=True, usage_hint="Usage: /q <prompt>")
    async def handle_queue_add(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /q <prompt> command - add command to FIFO queue."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        queue_entries: list[tuple[str, Optional[str], Optional[str], Optional[int]]]
        replace_pending = False
        if contains_queue_plan_markers(ctx.text):
            try:
                submission_options, plan_text = parse_queue_plan_submission(ctx.text)
                replace_pending = submission_options.replace_pending
                materialized_prompts = await materialize_queue_plan_text(
                    text=plan_text,
                    working_directory=session.working_directory,
                    git_service=git_service,
                )
            except QueuePlanError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Invalid structured queue plan: {e}",
                    blocks=error_message(f"Invalid structured queue plan: {e}"),
                )
                return
            queue_entries = [
                (
                    item.prompt,
                    item.working_directory_override,
                    item.parallel_group_id,
                    item.parallel_limit,
                )
                for item in materialized_prompts
            ]
        else:
            queue_entries = [(ctx.text, None, None, None)]

        queued_items = await deps.db.add_many_to_queue(
            session_id=session.id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            queue_entries=queue_entries,
            replace_pending=replace_pending,
        )

        running_items = await deps.db.get_running_queue_items(ctx.channel_id, ctx.thread_ts)
        position_offset = len(running_items)
        start_position = queued_items[0].position + position_offset
        end_position = queued_items[-1].position + position_offset
        item_count = len(queued_items)
        if start_position == end_position:
            position_text = f"position #{start_position}"
        else:
            position_text = f"positions #{start_position}-#{end_position}"
        queue_state = await _queue_state_for_submission(
            deps,
            ctx.channel_id,
            ctx.thread_ts,
            replace_pending=replace_pending,
        )
        paused_notice = _queue_state_notice(queue_state)
        action_verb = "Queued" if replace_pending else "Added"
        confirmation_text = f"{action_verb} {item_count} item(s) to queue ({position_text})."
        if paused_notice:
            confirmation_text = f"{confirmation_text} {paused_notice}"

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=confirmation_text,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":inbox_tray: {action_verb} {item_count} item(s) to queue ({position_text})\n"
                        f"> {escape_markdown(ctx.text[:200])}"
                        f"{'...' if len(ctx.text) > 200 else ''}",
                    },
                },
                *(
                    [
                        {
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": paused_notice}],
                        }
                    ]
                    if paused_notice
                    else []
                ),
            ],
        )

        if queue_state == "running":
            await ensure_queue_processor(
                ctx.channel_id, ctx.thread_ts, deps, ctx.client, ctx.logger
            )

    async def _post_queue_status(ctx: CommandContext, target_thread_ts: Optional[str]) -> None:
        pending = await deps.db.get_pending_queue_items(ctx.channel_id, target_thread_ts)
        running = await deps.db.get_running_queue_items(ctx.channel_id, target_thread_ts)
        queue_state = await _get_queue_state(deps, ctx.channel_id, target_thread_ts)
        blocks = queue_status(pending, running)
        if queue_state != "running":
            blocks.insert(
                2,
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": _queue_state_notice(queue_state)}],
                },
            )
        blocks.insert(
            2,
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Scope:* {_queue_scope_label(target_thread_ts)}"}
                ],
            },
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Queue status",
            blocks=blocks,
        )

    async def _post_channel_queue_overview(ctx: CommandContext) -> None:
        scope_thread_ids = await deps.db.list_queue_scopes_for_channel(ctx.channel_id)
        if None not in scope_thread_ids:
            scope_thread_ids = [None, *scope_thread_ids]

        scopes: list[dict[str, object]] = []
        for thread_ts in scope_thread_ids:
            pending = await deps.db.get_pending_queue_items(ctx.channel_id, thread_ts)
            running = await deps.db.get_running_queue_items(ctx.channel_id, thread_ts)
            queue_state = await _get_queue_state(deps, ctx.channel_id, thread_ts)
            if not pending and not running and queue_state == "running" and thread_ts is not None:
                continue

            preview = None
            if running:
                preview = running[0].prompt
            elif pending:
                preview = pending[0].prompt

            scopes.append(
                {
                    "label": _queue_scope_label(thread_ts),
                    "state": queue_state,
                    "running_count": len(running),
                    "pending_count": len(pending),
                    "preview": preview,
                }
            )

        blocks = queue_scope_overview(scopes)
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Slash commands may not include thread context. "
                            "Use `/qc view <thread_ts>` or `/qc stop <thread_ts>` "
                            "to target a thread queue explicitly."
                        ),
                    }
                ],
            }
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Queue status",
            blocks=blocks,
        )

    async def _clear_pending_queue(ctx: CommandContext) -> None:
        cleared = await deps.db.clear_queue(ctx.channel_id, ctx.thread_ts)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Cleared {cleared} item(s) from queue",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":wastebasket: Cleared {cleared} pending item(s) from queue.",
                    },
                },
            ],
        )

    async def _delete_entire_queue(ctx: CommandContext) -> None:
        await deps.db.update_queue_control_state(ctx.channel_id, ctx.thread_ts, "stopped")
        await TaskManager.cancel(_queue_task_id(ctx.channel_id, ctx.thread_ts))
        deleted = await deps.db.delete_queue(ctx.channel_id, ctx.thread_ts)
        await deps.db.update_queue_control_state(ctx.channel_id, ctx.thread_ts, "running")

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Deleted queue with {deleted} item(s)",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":wastebasket: Deleted the entire queue for this scope "
                            f"({deleted} item(s))."
                        ),
                    },
                },
            ],
        )

    async def _remove_pending_queue_item(ctx: CommandContext, item_id: Optional[int]) -> None:
        if item_id is None:
            pending = await deps.db.get_pending_queue_items(ctx.channel_id, ctx.thread_ts)
            if not pending:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Queue is empty",
                    blocks=error_message("Queue is empty. Nothing to remove."),
                )
                return
            item_id = pending[0].id

        removed = await deps.db.remove_queue_item(item_id, ctx.channel_id, ctx.thread_ts)

        if removed:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"Removed item #{item_id} from queue",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":wastebasket: Removed item #{item_id} from queue.",
                        },
                    },
                ],
            )
            return

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Item #{item_id} not found or not pending",
            blocks=error_message(f"Item #{item_id} not found or is already running/completed."),
        )

    @app.command("/qc")
    @slack_command(
        require_text=True,
        usage_hint="Usage: /qc <view|clear|delete|remove [item_id]|pause|stop|resume>",
    )
    async def handle_queue_command(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qc queue control subcommands."""
        parts = ctx.text.split()
        subcommand = parts[0].lower()
        args = parts[1:]

        if subcommand == "view":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc view [channel|thread_ts]"),
                )
                return
            if not args and ctx.thread_ts is None:
                await _post_channel_queue_overview(ctx)
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            await _post_queue_status(ctx, target_thread_ts)
            return

        if subcommand == "clear":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc clear [channel|thread_ts]"),
                )
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            original_thread_ts = ctx.thread_ts
            ctx.thread_ts = target_thread_ts
            await _clear_pending_queue(ctx)
            ctx.thread_ts = original_thread_ts
            return

        if subcommand == "delete":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc delete [channel|thread_ts]"),
                )
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            original_thread_ts = ctx.thread_ts
            ctx.thread_ts = target_thread_ts
            await _delete_entire_queue(ctx)
            ctx.thread_ts = original_thread_ts
            return

        if subcommand == "remove":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc remove [item_id]"),
                )
                return

            if args:
                try:
                    item_id = int(args[0])
                except ValueError:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="Invalid item ID",
                        blocks=error_message("Invalid item ID. Usage: /qc remove [item_id]"),
                    )
                    return
            else:
                item_id = None

            await _remove_pending_queue_item(ctx, item_id)
            return

        if subcommand == "pause":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc pause [channel|thread_ts]"),
                )
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            running_items = await deps.db.get_running_queue_items(ctx.channel_id, target_thread_ts)
            await deps.db.update_queue_control_state(ctx.channel_id, target_thread_ts, "paused")
            scope_label = _queue_scope_label(target_thread_ts)
            text = (
                f"{scope_label}: pause requested. Current item(s) will finish before stopping."
                if running_items
                else f"{scope_label}: paused."
            )
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=text,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    }
                ],
            )
            return

        if subcommand == "stop":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc stop [channel|thread_ts]"),
                )
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            await deps.db.update_queue_control_state(ctx.channel_id, target_thread_ts, "stopped")
            cancelled = await TaskManager.cancel(_queue_task_id(ctx.channel_id, target_thread_ts))
            scope_label = _queue_scope_label(target_thread_ts)
            text = (
                f"{scope_label}: stopped immediately." if cancelled else f"{scope_label}: stopped."
            )
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=text,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    }
                ],
            )
            return

        if subcommand == "resume":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc resume [channel|thread_ts]"),
                )
                return
            try:
                target_thread_ts = _parse_scope_selector(args[0]) if args else ctx.thread_ts
            except ValueError as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue scope",
                    blocks=error_message(str(e)),
                )
                return

            await deps.db.update_queue_control_state(ctx.channel_id, target_thread_ts, "running")
            recovered_stale_count = await _recover_stale_running_items(
                channel_id=ctx.channel_id,
                thread_ts=target_thread_ts,
                deps=deps,
                log=ctx.logger,
            )
            pending = await deps.db.get_pending_queue_items(ctx.channel_id, target_thread_ts)
            running_items = await deps.db.get_running_queue_items(ctx.channel_id, target_thread_ts)
            scope_label = _queue_scope_label(target_thread_ts)
            if pending:
                await ensure_queue_processor(
                    ctx.channel_id, target_thread_ts, deps, ctx.client, ctx.logger
                )
                if running_items:
                    text = (
                        f"{scope_label}: resumed. Existing running item(s) will continue and "
                        "pending work will follow."
                    )
                else:
                    text = f"{scope_label}: resumed. {len(pending)} pending item(s) ready to run."
                    if recovered_stale_count:
                        text = f"{text} Recovered {recovered_stale_count} stale running item(s)."
            elif running_items:
                text = (
                    f"{scope_label}: resumed. Existing running item(s) will continue and pending "
                    "work will follow."
                )
            else:
                text = f"{scope_label}: resumed. No pending items remain."
                if recovered_stale_count:
                    text = f"{text} Recovered {recovered_stale_count} stale running item(s)."
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=text,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    }
                ],
            )
            return

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Invalid queue command",
            blocks=error_message(
                "Usage: /qc <view|clear|delete|remove [item_id]|pause|stop|resume>"
            ),
        )

    @app.command("/qv")
    @slack_command()
    async def handle_queue_view(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qv command - view queue status."""
        if ctx.text:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Invalid queue command",
                blocks=error_message("Usage: /qv"),
            )
            return

        if ctx.thread_ts is None:
            await _post_channel_queue_overview(ctx)
            return

        await _post_queue_status(ctx, ctx.thread_ts)

    @app.command("/qclear")
    @slack_command()
    async def handle_queue_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qclear command - clear pending queue items."""
        if ctx.text:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Invalid queue command",
                blocks=error_message("Usage: /qclear"),
            )
            return

        await _clear_pending_queue(ctx)

    @app.command("/qdelete")
    @slack_command()
    async def handle_queue_delete(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qdelete command - delete all queue items in the current scope."""
        if ctx.text:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Invalid queue command",
                blocks=error_message("Usage: /qdelete"),
            )
            return

        await _delete_entire_queue(ctx)

    @app.command("/qr")
    @slack_command()
    async def handle_queue_remove(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qr command - remove next queue item or specific item by id."""
        if ctx.text:
            parts = ctx.text.split()
            if len(parts) != 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qr [item_id]"),
                )
                return
            try:
                item_id = int(parts[0])
            except ValueError:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid item ID",
                    blocks=error_message("Invalid item ID. Usage: /qr [item_id]"),
                )
                return
        else:
            item_id = None

        await _remove_pending_queue_item(ctx, item_id)


async def _process_queue(
    channel_id: str,
    deps: HandlerDependencies,
    client,
    task_logger,
    thread_ts: Optional[str] = None,
) -> None:
    """Process queue items for a channel/thread scope."""
    log = task_logger or logger
    scope = build_session_scope(channel_id, thread_ts)
    task_id = _queue_task_id(channel_id, thread_ts)
    override_resume_ids: dict[str, dict[str, str]] = {}
    processed_count = 0
    status_counts = {"completed": 0, "failed": 0, "cancelled": 0}
    final_queue_state = "running"
    remaining_pending = 0

    try:
        while True:
            try:
                final_queue_state = await _get_queue_state(deps, channel_id, thread_ts)
                if final_queue_state != "running":
                    remaining_pending = len(
                        await deps.db.get_pending_queue_items(channel_id, thread_ts)
                    )
                    log.info(
                        f"Queue processor halting for scope {scope} because state="
                        f"{final_queue_state}"
                    )
                    break

                # Ensure we never overlap with a currently running Codex turn in this scope.
                active_turn_wait_started_at: float | None = None
                next_wait_log_at: float = 0.0
                while deps.codex_executor and await deps.codex_executor.has_active_turn(scope):
                    now = time.monotonic()
                    if active_turn_wait_started_at is None:
                        active_turn_wait_started_at = now
                        next_wait_log_at = now + 30.0
                        log.info(f"Queue waiting for active Codex turn to finish in scope {scope}")
                    elif now >= next_wait_log_at:
                        waited = now - active_turn_wait_started_at
                        log.info(
                            f"Queue still waiting for active Codex turn in scope "
                            f"{scope} (waited {waited:.1f}s)"
                        )
                        next_wait_log_at = now + 30.0
                    await asyncio.sleep(0.5)
                if active_turn_wait_started_at is not None:
                    waited = time.monotonic() - active_turn_wait_started_at
                    log.info(
                        f"Queue resumed after active Codex turn finished in scope "
                        f"{scope} (waited {waited:.1f}s)"
                    )

                # Fetch after waiting so we do not act on stale pending snapshots.
                pending = await deps.db.get_pending_queue_items(channel_id, thread_ts)
                if not pending:
                    remaining_pending = 0
                    final_queue_state = await _get_queue_state(deps, channel_id, thread_ts)
                    log.info(f"Queue empty for scope {scope}, stopping processor")
                    break

                item = pending[0]
                session = await deps.db.get_or_create_session(
                    channel_id,
                    thread_ts=thread_ts,
                    default_cwd=config.DEFAULT_WORKING_DIR,
                )
                if item.parallel_group_id:
                    group_items = await deps.db.get_queue_group_items(
                        channel_id,
                        thread_ts,
                        item.parallel_group_id,
                        statuses=("pending",),
                    )
                    group_statuses = await _run_parallel_group(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        scope=scope,
                        deps=deps,
                        client=client,
                        log=log,
                        session=session,
                        items=group_items,
                    )
                    for status in group_statuses:
                        status_counts[status] = status_counts.get(status, 0) + 1
                else:
                    processed_count += 1
                    status = await _execute_queue_item(
                        item,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        scope=scope,
                        deps=deps,
                        client=client,
                        log=log,
                        base_session=session,
                        sequence_label=str(processed_count),
                        override_resume_ids=override_resume_ids,
                    )
                    if status:
                        status_counts[status] = status_counts.get(status, 0) + 1
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as loop_error:
                # Keep processor alive for transient scope-level failures
                # (DB/network hiccups) instead of exiting permanently.
                log.error(f"Queue processor transient error in scope {scope}: {loop_error}")
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        log.info(f"Queue processor cancelled for scope {scope}")
        raise
    finally:
        if sum(status_counts.values()) > 0:
            final_queue_state = await _get_queue_state(deps, channel_id, thread_ts)
            if final_queue_state in {"paused", "stopped"}:
                remaining_pending = len(
                    await deps.db.get_pending_queue_items(channel_id, thread_ts)
                )
                completion_text = _build_queue_halted_text(
                    final_queue_state, status_counts, remaining_pending
                )
            else:
                completion_text = _build_queue_completion_text(status_counts)
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=completion_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":white_check_mark: {completion_text}"
                                    if final_queue_state == "running"
                                    else completion_text
                                ),
                            },
                        }
                    ],
                )
            except Exception as notify_error:
                log.error(
                    f"Failed to post queue completion notification for scope {scope}: "
                    f"{notify_error}"
                )
        await _cleanup_queue_start_lock(task_id)
