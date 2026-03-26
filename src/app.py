#!/usr/bin/env python3
"""
Slack Claude Code Bot - Main Application Entry Point

A Slack app that allows running Claude Code CLI commands from Slack,
with each channel representing a separate session.
"""

import asyncio
import os
import random
import re
import signal
import sys
import time
import traceback
from dataclasses import replace
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from src.approval.plan_manager import PlanApprovalManager
from src.claude.subprocess_executor import SubprocessExecutor as ClaudeExecutor
from src.codex.subprocess_executor import SubprocessExecutor as CodexExecutor
from src.config import config, get_backend_for_model
from src.database.migrations import init_database
from src.database.repository import DatabaseRepository
from src.handlers import register_commands
from src.handlers.actions import register_actions
from src.handlers.claude.queue import (
    ensure_queue_processor,
    ensure_queue_schedule_dispatcher,
)
from src.handlers.execution_runtime import execute_prompt_with_runtime
from src.question.manager import QuestionManager
from src.tasks.queue_plan import (
    QueuePlanError,
    QueueScheduledControl,
    contains_queue_plan_markers,
    materialize_queue_plan_text,
    parse_queue_plan_submission,
)
from src.utils.execution_scope import build_session_scope
from src.utils.file_downloader import (
    FileDownloadError,
    FileTooLargeError,
    download_slack_file,
)
from src.utils.formatters.command import error_message
from src.utils.mode_directives import (
    ModeDirectiveError,
    RuntimeModeOverrides,
    parse_parenthesized_mode_directive_line,
    resolve_runtime_mode_value,
)

_TEXT_MIME_TYPES_FOR_QUEUE_PLAN = {
    "application/json",
    "application/toml",
    "application/x-yaml",
    "application/xml",
}
_TEXT_FILE_EXTENSIONS_FOR_QUEUE_PLAN = {
    ".cfg",
    ".conf",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".markdown",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_RUNTIME_MODE_DIRECTIVE_META_KEY = "runtime_mode_directive"


def configure_logging() -> None:
    """Configure log sinks for stderr and data-directory log file."""
    data_dir = _application_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "slack_claude.log"

    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    logger.add(
        log_path,
        level="DEBUG",
        rotation="00:00",
        retention="3 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


def _apply_runtime_mode_overrides_to_session(
    session,
    overrides: RuntimeModeOverrides,
):
    """Return a session clone with ephemeral runtime mode overrides applied."""
    replace_kwargs: dict[str, str] = {}
    if overrides.permission_mode is not None:
        replace_kwargs["permission_mode"] = overrides.permission_mode
    if overrides.approval_mode is not None:
        replace_kwargs["approval_mode"] = overrides.approval_mode
    if overrides.sandbox_mode is not None:
        replace_kwargs["sandbox_mode"] = overrides.sandbox_mode
    if not replace_kwargs:
        return session
    return replace(session, **replace_kwargs)


def _extract_single_prompt_mode_directive(prompt: str) -> tuple[str, Optional[str]]:
    """Extract one leading `(mode: ...)` directive for non-structured prompts."""
    lines = prompt.splitlines()
    if not lines:
        return prompt, None

    mode_directive = parse_parenthesized_mode_directive_line(lines[0].strip())
    if mode_directive is None:
        return prompt, None

    remaining_lines = lines[1:]
    end_indices = [
        idx
        for idx, line in enumerate(remaining_lines)
        if line.strip().lower() in {"(end)", "((end))"}
    ]
    if end_indices:
        last_non_empty_index = max(
            idx
            for idx, line in enumerate(remaining_lines)
            if line.strip()
        )
        first_end_index = end_indices[0]
        if first_end_index != last_non_empty_index:
            raise ModeDirectiveError(
                "When using `(mode: ...)` in a regular prompt, `(end)` must be the final "
                "non-empty line."
            )
        if len(end_indices) > 1:
            raise ModeDirectiveError(
                "When using `(mode: ...)` in a regular prompt, only one closing `(end)` "
                "marker is supported."
            )
        del remaining_lines[first_end_index]

    stripped_prompt = "\n".join(remaining_lines).strip()
    if not stripped_prompt:
        raise ModeDirectiveError("Mode directive must be followed by prompt content.")
    if contains_queue_plan_markers(stripped_prompt):
        # Defer to structured queue-plan parser when marker semantics remain.
        return prompt, None
    return stripped_prompt, mode_directive


def _application_data_dir() -> Path:
    """Return the app's persistent data directory."""
    return Path(config.DATABASE_PATH).expanduser().resolve().parent


def _result_field(result: Any, field_name: str, default: Any) -> Any:
    """Read a field from dataclass/SimpleNamespace-like values safely."""
    try:
        values = vars(result)
    except TypeError:
        return default
    if field_name in values:
        return values[field_name]
    return default


def _slack_uploads_dir() -> Path:
    """Return the directory used for persisted Slack uploads."""
    return _application_data_dir() / "slack_uploads"


async def slack_api_with_retry(
    api_call,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """
    Execute a Slack API call with retry logic for transient failures.

    Handles both SlackApiError and network errors (TimeoutError, CancelledError).

    Args:
        api_call: Async callable that performs the Slack API call
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff

    Returns:
        The result of the API call

    Raises:
        The last exception if all retries fail
    """
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    last_error = None
    for attempt in range(max_retries):
        try:
            return await api_call()
        except asyncio.CancelledError:
            raise
        except (SlackApiError, TimeoutError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Slack API error (attempt {attempt + 1}/{max_retries}): "
                    f"{type(e).__name__}: {e}, retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"Slack API call failed after {max_retries} attempts: "
                    f"{type(e).__name__}: {e}"
                )
                raise
    raise last_error


async def shutdown(
    claude_executor: ClaudeExecutor,
    codex_executor: CodexExecutor | None = None,
) -> None:
    """Graceful shutdown: cleanup active processes."""
    logger.info("Shutting down - cleaning up active processes...")
    await claude_executor.shutdown()
    if codex_executor:
        await codex_executor.shutdown()
    logger.info("All processes terminated")


async def post_channel_notification(
    client,
    db: DatabaseRepository,
    channel_id: str,
    thread_ts: str | None,
    notification_type: str,
    max_retries: int = 3,
) -> None:
    """
    Post a brief notification to the channel (not thread) to trigger Slack sounds and unread badges.

    Args:
        client: Slack WebClient
        db: Database repository
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (for linking)
        notification_type: "completion" or "permission"
        max_retries: Maximum number of retry attempts (default: 3)
    """
    try:
        settings = await db.get_notification_settings(channel_id)

        if notification_type == "completion" and not settings.notify_on_completion:
            return
        elif notification_type == "permission" and not settings.notify_on_permission:
            return

        # Build thread link if we have a thread_ts
        if thread_ts:
            thread_link = (
                f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
            )
            if notification_type == "completion":
                message = f"✅ Assistant finished • <{thread_link}|View thread>"
            else:
                message = (
                    f"⚠️ Assistant needs permission • <{thread_link}|Respond in thread>"
                )
        else:
            if notification_type == "completion":
                message = "✅ Assistant finished"
            else:
                message = "⚠️ Assistant needs permission"

        await slack_api_with_retry(
            lambda: client.chat_postMessage(
                channel=channel_id,
                text=message,
            ),
            max_retries=max_retries,
        )
        logger.debug(f"Posted {notification_type} notification to channel {channel_id}")

    except Exception as e:
        # Don't fail the main operation if all notification attempts fail
        logger.error(
            f"Failed to post channel notification after {max_retries} attempts: {e}"
        )


def _strip_leading_slack_mention(text: str) -> str:
    """Strip one leading Slack mention token (e.g., <@U123>) from message text."""
    if not text:
        return ""
    return re.sub(r"^\s*<@[^>\s]+>\s*", "", text, count=1).strip()


def _is_text_upload_for_queue_plan(uploaded_file) -> bool:
    """Return True when uploaded file is likely readable text for queue-plan parsing."""
    mimetype = (uploaded_file.mimetype or "").strip().lower()
    if not mimetype:
        return True
    if mimetype.startswith("text/"):
        return True
    if mimetype in _TEXT_MIME_TYPES_FOR_QUEUE_PLAN:
        return True

    suffix = Path(uploaded_file.local_path).suffix.strip().lower()
    return suffix in _TEXT_FILE_EXTENSIONS_FOR_QUEUE_PLAN


def _extract_structured_queue_plan_from_uploaded_files(
    uploaded_files: list, logger
) -> str | None:
    """Return first uploaded text file content that looks like a structured queue plan."""
    for uploaded_file in uploaded_files:
        if not _is_text_upload_for_queue_plan(uploaded_file):
            continue
        try:
            content = Path(uploaded_file.local_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.debug(
                "Skipping uploaded file for queue-plan parsing due to UTF-8 decode failure: "
                f"{uploaded_file.local_path}"
            )
            continue
        except OSError as e:
            logger.warning(
                "Failed reading uploaded file for queue-plan parsing: "
                f"{uploaded_file.local_path} ({e})"
            )
            continue

        if contains_queue_plan_markers(content):
            logger.info(
                "Detected structured queue-plan markers in uploaded file: "
                f"{uploaded_file.filename}"
            )
            return content

    return None


def _event_dedupe_key(event: dict[str, Any]) -> str | None:
    """Build a stable dedupe key for inbound Slack message events."""
    channel_id = event.get("channel")
    message_ts = event.get("ts")
    if not channel_id or not message_ts:
        return None
    user_id = event.get("user") or ""
    return f"{channel_id}:{message_ts}:{user_id}"


def _is_duplicate_event(
    event: dict[str, Any],
    seen_events: dict[str, float],
    now_monotonic: float,
    ttl_seconds: float,
) -> bool:
    """Return True when this event key has already been seen within the dedupe window."""
    if ttl_seconds > 0:
        cutoff = now_monotonic - ttl_seconds
        expired_keys = [key for key, seen_at in seen_events.items() if seen_at < cutoff]
        for key in expired_keys:
            del seen_events[key]

    event_key = _event_dedupe_key(event)
    if event_key is None:
        return False
    if event_key in seen_events:
        return True
    seen_events[event_key] = now_monotonic
    return False


def _queue_state_notice(state: str) -> str:
    """Return a short operator-facing notice for a non-running queue."""
    if state == "paused":
        return "Queue is paused. Use `/qc resume` to continue."
    if state == "stopped":
        return "Queue is stopped. Use `/qc resume` to continue."
    return ""


def _format_scheduled_event_timestamp(event_time) -> str:
    """Format scheduled event timestamps in UTC for operator visibility."""
    if event_time.tzinfo is None or event_time.tzinfo.utcoffset(event_time) is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _scheduled_controls_summary(controls: list[QueueScheduledControl]) -> str:
    """Build a short queue scheduled controls summary for confirmations."""
    if not controls:
        return ""
    parts = [
        f"{control.action} at {_format_scheduled_event_timestamp(control.execute_at)}"
        for control in controls[:3]
    ]
    summary = ", ".join(parts)
    if len(controls) > 3:
        summary = f"{summary}, and {len(controls) - 3} more"
    return f"Scheduled controls: {summary}."


async def _queue_state_for_submission(
    deps,
    channel_id: str,
    thread_ts: str | None,
    replace_pending: bool,
) -> str:
    """Return effective queue state for a new submission.

    Replacing pending items starts a new queue generation, so any prior
    pause/stop control should not block the replacement queue from running.
    """
    queue_state = (await deps.db.get_queue_control(channel_id, thread_ts)).state
    if replace_pending and queue_state != "running":
        queue_state = (
            await deps.db.update_queue_control_state(channel_id, thread_ts, "running")
        ).state
    return queue_state


async def _handle_typed_model_command(
    client,
    channel_id: str,
    thread_ts: str | None,
    message_ts: str | None,
) -> None:
    """Guide users to use the Slack slash command when /model is typed as plain text."""
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts or message_ts,
        text="Use `/model` slash command to open the model selector.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":information_source: `/model` was sent as regular message text, "
                        "so it was treated like a normal prompt.\n"
                        "Run the Slack slash command `/model` to open the model selector."
                    ),
                },
            }
        ],
    )


async def _post_message_processing_error(
    client,
    channel_id: str,
    thread_ts: str | None,
    error_text: str,
) -> None:
    """Post a best-effort Slack notice for an unexpected message-processing failure."""
    summary_limit = 240
    detail_limit = 1500
    summary = error_text[:summary_limit]
    if len(error_text) > summary_limit:
        summary = f"{summary}..."
    details = error_text[:detail_limit]
    if len(error_text) > detail_limit:
        details = f"{details}..."

    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Error: {summary}",
        blocks=error_message(
            "Unexpected error while processing this message.\n" f"{details}"
        ),
    )


async def _route_codex_message_to_active_turn_or_queue(
    client,
    deps,
    session,
    channel_id: str,
    thread_ts: str | None,
    prompt: str,
    logger,
    runtime_mode_directive: Optional[str] = None,
) -> bool:
    """Route a Codex message to an active turn, or queue it on steer failure.

    Returns
    -------
    bool
        True when the message was handled by steer or queue fallback, False when
        no active turn exists and normal execution should continue.
    """
    if not deps.codex_executor:
        return False

    session_scope = build_session_scope(channel_id, thread_ts)
    if not await deps.codex_executor.has_active_turn(session_scope):
        return False

    cmd_history = await deps.db.add_command(session.id, prompt)
    await deps.db.update_command_status(cmd_history.id, "running")

    steer_error: str | None = None
    steer_result = None
    if runtime_mode_directive is None:
        try:
            steer_result = await deps.codex_executor.steer_active_turn(
                session_scope=session_scope,
                text=prompt,
            )
        except Exception as e:
            steer_error = str(e)
            logger.error(
                f"Failed to steer active Codex turn in scope {session_scope}: {steer_error}"
            )
    else:
        steer_error = (
            "Active Codex turn steering is skipped when `(mode: ...)` overrides are present."
        )

    if steer_result and steer_result.success:
        await deps.db.update_command_status(
            cmd_history.id,
            "completed",
            output=(
                "Routed to active Codex turn via turn/steer."
                f" turn_id={steer_result.turn_id or 'unknown'}"
            ),
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Message merged into active Codex execution.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":compass: Routed your message to the active Codex run "
                            "using `turn/steer`."
                        ),
                    },
                }
            ],
        )
        return True

    steer_error = steer_error or (
        steer_result.error if steer_result else "unknown error"
    )
    try:
        queue_meta = (
            {_RUNTIME_MODE_DIRECTIVE_META_KEY: runtime_mode_directive}
            if runtime_mode_directive
            else None
        )
        queue_kwargs = {
            "session_id": session.id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "prompt": prompt,
        }
        if queue_meta is not None:
            queue_kwargs["automation_meta"] = queue_meta
        queued_item = await deps.db.add_to_queue(**queue_kwargs)
        await deps.codex_executor.record_queue_fallback(success=True)
    except Exception as e:
        await deps.codex_executor.record_queue_fallback(success=False)
        queue_error = str(e)
        await deps.db.update_command_status(
            cmd_history.id,
            "failed",
            output=(
                "Steer failed and queue fallback failed."
                f" steer_error={steer_error} queue_error={queue_error}"
            ),
            error_message=queue_error,
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Failed to queue message after steer failure.",
            blocks=error_message(
                "Active Codex run could not be steered and queue fallback failed.\n"
                f"steer_error: {steer_error}\nqueue_error: {queue_error}"
            ),
        )
        return True

    await deps.db.update_command_status(
        cmd_history.id,
        "completed",
        output=(
            f"Steer failed ({steer_error}). " f"Auto-queued item #{queued_item.id}."
        ),
    )
    try:
        await ensure_queue_processor(
            channel_id=channel_id,
            thread_ts=thread_ts,
            deps=deps,
            client=client,
            task_logger=logger,
        )
    except Exception as e:
        queue_start_error = str(e)
        logger.error(
            f"Queued Codex fallback item #{queued_item.id} for scope {session_scope} "
            f"but failed to start queue processor: {queue_start_error}"
        )
        await deps.db.update_command_status(
            cmd_history.id,
            "failed",
            output=(
                f"Steer failed ({steer_error}). Auto-queued item #{queued_item.id}, "
                f"but queue processor startup failed: {queue_start_error}"
            ),
            error_message=queue_start_error,
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Queued item #{queued_item.id}, but queue startup failed.",
            blocks=error_message(
                "Active Codex run could not be steered, so your message was queued, "
                "but the queue processor failed to start.\n"
                f"queued_item: #{queued_item.id}\n"
                f"queue_start_error: {queue_start_error}"
            ),
        )
        return True
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Steer unavailable; queued message as item #{queued_item.id}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":inbox_tray: Active Codex run is busy and steering failed.\n"
                        f"Queued as item *#{queued_item.id}* in this session scope."
                    ),
                },
            }
        ],
    )
    return True


async def _route_claude_message_to_active_execution_or_queue(
    client,
    deps,
    session,
    channel_id: str,
    thread_ts: str | None,
    prompt: str,
    logger,
    runtime_mode_directive: Optional[str] = None,
) -> bool:
    """Route a Claude message to queue when an execution is already active in scope.

    Returns
    -------
    bool
        True when the message was handled by queue fallback, False when no
        active execution exists and normal execution should continue.
    """
    session_scope = build_session_scope(channel_id, thread_ts)
    if not await deps.executor.has_active_execution(session_scope):
        return False

    cmd_history = await deps.db.add_command(session.id, prompt)
    await deps.db.update_command_status(cmd_history.id, "running")

    try:
        queue_meta = (
            {_RUNTIME_MODE_DIRECTIVE_META_KEY: runtime_mode_directive}
            if runtime_mode_directive
            else None
        )
        queue_kwargs = {
            "session_id": session.id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "prompt": prompt,
        }
        if queue_meta is not None:
            queue_kwargs["automation_meta"] = queue_meta
        queued_item = await deps.db.add_to_queue(**queue_kwargs)
    except Exception as e:
        queue_error = str(e)
        logger.error(
            f"Failed to queue message while Claude execution was active in scope "
            f"{session_scope}: {queue_error}"
        )
        await deps.db.update_command_status(
            cmd_history.id,
            "failed",
            output=(
                "Active Claude execution detected but queue fallback failed."
                f" queue_error={queue_error}"
            ),
            error_message=queue_error,
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Failed to queue message while Claude run is active.",
            blocks=error_message(
                "Active Claude execution is already running and auto-queue failed.\n"
                f"queue_error: {queue_error}"
            ),
        )
        return True

    await deps.db.update_command_status(
        cmd_history.id,
        "completed",
        output=(
            "Active Claude execution detected." f" Auto-queued item #{queued_item.id}."
        ),
    )
    try:
        await ensure_queue_processor(
            channel_id=channel_id,
            thread_ts=thread_ts,
            deps=deps,
            client=client,
            task_logger=logger,
        )
    except Exception as e:
        queue_start_error = str(e)
        logger.error(
            f"Queued Claude fallback item #{queued_item.id} for scope {session_scope} "
            f"but failed to start queue processor: {queue_start_error}"
        )
        await deps.db.update_command_status(
            cmd_history.id,
            "failed",
            output=(
                f"Active Claude execution detected. Auto-queued item #{queued_item.id}, "
                f"but queue processor startup failed: {queue_start_error}"
            ),
            error_message=queue_start_error,
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Queued item #{queued_item.id}, but queue startup failed.",
            blocks=error_message(
                "A Claude run is already active, so your message was queued, "
                "but the queue processor failed to start.\n"
                f"queued_item: #{queued_item.id}\n"
                f"queue_start_error: {queue_start_error}"
            ),
        )
        return True
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Claude run active; queued message as item #{queued_item.id}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":inbox_tray: A Claude run is already active in this session scope.\n"
                        f"Queued as item *#{queued_item.id}*."
                    ),
                },
            }
        ],
    )
    return True


async def _queue_structured_plan_message(
    client,
    deps,
    session,
    channel_id: str,
    thread_ts: str | None,
    prompt: str,
    logger,
) -> bool:
    """Parse structured queue plan text and enqueue generated items when markers are present."""
    if not contains_queue_plan_markers(prompt):
        return False

    scheduled_controls: list[QueueScheduledControl] = []
    replace_pending = False
    insertion_mode = "append"
    insert_at = None
    auto_after_each_prompt = False
    auto_after_queue_finish = False
    try:
        submission_options, plan_text = parse_queue_plan_submission(prompt)
        replace_pending = bool(getattr(submission_options, "replace_pending", False))
        insertion_mode = str(getattr(submission_options, "insertion_mode", "append"))
        insert_at = getattr(submission_options, "insert_at", None)
        scheduled_controls = list(getattr(submission_options, "scheduled_controls", []))
        auto_after_each_prompt = bool(
            getattr(submission_options, "auto_after_each_prompt", False)
        )
        auto_after_queue_finish = bool(
            getattr(submission_options, "auto_after_queue_finish", False)
        )
        materialized_prompts = await materialize_queue_plan_text(
            text=plan_text,
            working_directory=session.working_directory,
        )
    except QueuePlanError as e:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Invalid structured queue plan: {e}",
            blocks=error_message(f"Invalid structured queue plan: {e}"),
        )
        return True
    except Exception as e:
        logger.error(f"Failed to parse structured queue plan: {e}")
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Failed to process structured queue plan: {e}",
            blocks=error_message(f"Failed to process structured queue plan: {e}"),
        )
        return True

    try:
        if auto_after_each_prompt:
            queue_entries = []
            for item in materialized_prompts:
                entry_meta: dict[str, object] = {
                    "origin": "manual",
                    "auto_each": True,
                    "continue_round": 0,
                    "check_round": 0,
                }
                mode_directive = _result_field(item, "mode_directive", None)
                if isinstance(mode_directive, str) and mode_directive.strip():
                    entry_meta[_RUNTIME_MODE_DIRECTIVE_META_KEY] = mode_directive.strip()
                queue_entries.append(
                    (
                        item.prompt,
                        item.working_directory_override,
                        item.parallel_group_id,
                        item.parallel_limit,
                        entry_meta,
                    )
                )
        else:
            queue_entries = []
            for item in materialized_prompts:
                mode_directive = _result_field(item, "mode_directive", None)
                if isinstance(mode_directive, str) and mode_directive.strip():
                    queue_entries.append(
                        (
                            item.prompt,
                            item.working_directory_override,
                            item.parallel_group_id,
                            item.parallel_limit,
                            {
                                _RUNTIME_MODE_DIRECTIVE_META_KEY: mode_directive.strip(),
                            },
                        )
                    )
                else:
                    queue_entries.append(
                        (
                            item.prompt,
                            item.working_directory_override,
                            item.parallel_group_id,
                            item.parallel_limit,
                        )
                    )
        queued_items = await deps.db.add_many_to_queue(
            session_id=session.id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            queue_entries=queue_entries,
            replace_pending=replace_pending,
            insertion_mode=insertion_mode,
            insert_at=insert_at,
        )
        if scheduled_controls:
            await deps.db.update_queue_control_state(channel_id, thread_ts, "paused")
        if scheduled_controls:
            await deps.db.add_queue_scheduled_events(
                channel_id=channel_id,
                thread_ts=thread_ts,
                events=[
                    (control.action, control.execute_at)
                    for control in scheduled_controls
                ],
            )
        if auto_after_queue_finish:
            try:
                await deps.db.set_queue_auto_finish_pending(channel_id, thread_ts, True)
            except AttributeError:
                pass

        running = await deps.db.get_running_queue_items(channel_id, thread_ts)
        queue_state = await _queue_state_for_submission(
            deps,
            channel_id,
            thread_ts,
            replace_pending=replace_pending and not scheduled_controls,
        )
        if insertion_mode == "prepend":
            start_position = len(running) + 1
        elif insertion_mode == "insert" and insert_at is not None:
            start_position = len(running) + max(1, insert_at)
        else:
            start_position = len(running) + 1
        end_position = start_position + len(queued_items) - 1

        if queue_state == "running":
            await ensure_queue_processor(
                channel_id=channel_id,
                thread_ts=thread_ts,
                deps=deps,
                client=client,
                task_logger=logger,
            )
        if scheduled_controls:
            await ensure_queue_schedule_dispatcher(
                deps=deps,
                client=client,
                task_logger=logger,
            )
    except Exception as e:
        logger.error(f"Failed to enqueue structured queue plan: {e}")
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Failed to queue structured plan: {e}",
            blocks=error_message(f"Failed to queue structured plan: {e}"),
        )
        return True

    position_text = (
        f"position #{start_position}"
        if start_position == end_position
        else f"positions #{start_position}-#{end_position}"
    )
    item_count = len(queued_items)
    paused_notice = _queue_state_notice(queue_state)
    scheduled_summary = _scheduled_controls_summary(scheduled_controls)
    if replace_pending:
        action_verb = "Queued"
    elif insertion_mode == "prepend":
        action_verb = "Prepended"
    elif insertion_mode == "insert":
        action_verb = "Inserted"
    else:
        action_verb = "Added"
    confirmation_text = f"{action_verb} {item_count} item(s) from structured plan."
    if paused_notice:
        confirmation_text = f"{confirmation_text} {paused_notice}"
    if scheduled_summary:
        confirmation_text = f"{confirmation_text} {scheduled_summary}"
    auto_summary_parts: list[str] = []
    if auto_after_each_prompt:
        auto_summary_parts.append("Auto checks/continuation enabled after each prompt.")
    if auto_after_queue_finish:
        auto_summary_parts.append(
            "Auto-finish checks/continuation enabled for queue drain."
        )
    auto_summary = " ".join(auto_summary_parts).strip()
    if auto_summary:
        confirmation_text = f"{confirmation_text} {auto_summary}"

    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=confirmation_text,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":inbox_tray: "
                        f"{action_verb} "
                        f"*{item_count}* item(s) from structured plan ({position_text})."
                    ),
                },
            },
            *(
                [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": scheduled_summary}],
                    }
                ]
                if scheduled_summary
                else []
            ),
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
            *(
                [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": auto_summary}],
                    }
                ]
                if auto_summary
                else []
            ),
        ],
    )
    return True


async def _restore_pending_queue_processors(client, deps, logger) -> None:
    """Restart queue processors for scopes with pending work after process startup."""
    pending_scopes = await deps.db.list_pending_queue_scopes()
    if not pending_scopes:
        return

    logger.info(
        f"Startup queue recovery found {len(pending_scopes)} scope(s) with pending items"
    )
    started_count = 0
    skipped_count = 0
    for channel_id, thread_ts in pending_scopes:
        queue_state = (await deps.db.get_queue_control(channel_id, thread_ts)).state
        scope = build_session_scope(channel_id, thread_ts)
        if queue_state != "running":
            skipped_count += 1
            logger.info(
                f"Skipping startup queue recovery for scope {scope} because state={queue_state}"
            )
            continue
        await ensure_queue_processor(
            channel_id=channel_id,
            thread_ts=thread_ts,
            deps=deps,
            client=client,
            task_logger=logger,
        )
        started_count += 1

    logger.info(
        f"Startup queue recovery complete: started {started_count} processor(s), "
        f"skipped {skipped_count} non-running scope(s)"
    )


async def main():
    """Main application entry point."""
    configure_logging()

    # Validate configuration
    errors = config.validate_required()
    if errors:
        logger.error("Configuration errors:")
        for error in errors:
            logger.error(f"  - {error}")
        sys.exit(1)

    # Initialize database
    logger.info(f"Initializing database at {config.DATABASE_PATH}")
    await init_database(config.DATABASE_PATH)

    # Create app components
    db = DatabaseRepository(config.DATABASE_PATH)
    restored_channel_models = await db.restore_channel_model_selections()
    if restored_channel_models:
        logger.info(
            f"Restored {len(restored_channel_models)} channel model selection(s) from database"
        )

    # Initialize Claude executor (always available)
    claude_executor = ClaudeExecutor(db=db)

    # Initialize Codex executor (optional)
    codex_executor = CodexExecutor(db=db)

    # Create Slack app
    app = AsyncApp(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    # Register handlers (both Claude and Codex)
    deps = register_commands(
        app,
        db,
        claude_executor,
        codex_executor=codex_executor,
    )
    register_actions(app, deps)
    recent_message_events: dict[str, float] = {}
    event_dedupe_ttl_seconds = 30.0

    # Add a simple health check
    @app.event("app_mention")
    async def handle_mention(event, client, logger):
        """Handle @mentions by routing to message processing."""
        mention_prompt = _strip_leading_slack_mention(event.get("text", ""))
        if not mention_prompt:
            if _is_duplicate_event(
                event=event,
                seen_events=recent_message_events,
                now_monotonic=time.monotonic(),
                ttl_seconds=event_dedupe_ttl_seconds,
            ):
                logger.debug(
                    f"Skipping duplicate app_mention event for channel={event.get('channel')} ts={event.get('ts')}"
                )
                return
            await client.chat_postMessage(
                channel=event.get("channel"),
                thread_ts=event.get("thread_ts") or event.get("ts"),
                text="Hi! I'm the code assistant bot. Send me a message and I'll process it.",
            )
            return

        routed_event = dict(event)
        routed_event["text"] = mention_prompt
        await handle_message(routed_event, client, logger)

    @app.event("message")
    async def handle_message(event, client, logger):
        """Handle messages and pipe them to Claude Code."""
        if _is_duplicate_event(
            event=event,
            seen_events=recent_message_events,
            now_monotonic=time.monotonic(),
            ttl_seconds=event_dedupe_ttl_seconds,
        ):
            logger.debug(
                f"Skipping duplicate message event for channel={event.get('channel')} ts={event.get('ts')}"
            )
            return

        logger.info(f"Message event received: {event.get('text', '')[:50]}...")

        # Ignore bot messages to avoid responding to ourselves
        if event.get("bot_id"):
            logger.debug(f"Ignoring bot message: bot_id={event.get('bot_id')}")
            return
        if event.get("hidden"):
            logger.debug("Ignoring hidden message event")
            return

        # Ignore system subtypes but allow user messages with subtypes (e.g., file_share from mobile)
        ignored_subtypes = {
            "bot_message",
            "message_changed",
            "message_replied",
            "message_deleted",
            "channel_join",
            "channel_leave",
            "channel_topic",
            "channel_purpose",
            "channel_name",
            "channel_archive",
            "channel_unarchive",
            "ekm_access_denied",
            "me_message",
        }
        subtype = event.get("subtype")
        if subtype and subtype in ignored_subtypes:
            logger.debug(f"Ignoring system subtype message: subtype={subtype}")
            return

        channel_id = event.get("channel")
        user_id = event.get("user")  # Extract user ID for plan approval
        thread_ts = event.get("thread_ts")  # Extract thread timestamp
        prompt = _strip_leading_slack_mention(event.get("text", ""))
        # Extract uploaded files - ensure it's always a list
        files_data = event.get("files")
        files: list = files_data if isinstance(files_data, list) else []

        # Allow messages with files but no text
        if not prompt and not files:
            logger.debug("Empty prompt and no files, ignoring")
            return

        normalized_prompt = prompt.strip().lower()
        if normalized_prompt == "/model" or normalized_prompt.startswith("/model "):
            logger.info(
                "Detected typed /model message; redirecting user to slash command handler"
            )
            await _handle_typed_model_command(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=event.get("ts"),
            )
            return

        # Get or create session (thread-aware)
        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        logger.info(f"Using session: {session.session_display_name()}")

        # Cancel any pending questions for this session - unblocks handlers stuck
        # at wait_for_answer() when user sends a new message instead of clicking buttons
        cancelled_questions = await QuestionManager.cancel_for_session(str(session.id))
        if cancelled_questions:
            logger.info(
                f"Cancelled {cancelled_questions} pending question(s) for session {session.id} "
                "due to new message"
            )
        cancelled_plan_approvals = await PlanApprovalManager.cancel_for_session(
            str(session.id)
        )
        if cancelled_plan_approvals:
            logger.info(
                f"Cancelled {cancelled_plan_approvals} pending plan approval(s) for session {session.id} "
                "due to new message"
            )

        # Validate working directory exists
        if not os.path.isdir(session.working_directory):
            logger.error(
                f"Working directory does not exist: {session.working_directory}"
            )
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"⚠️ Working directory does not exist: `{session.working_directory}`\n\nUse `/cd <path>` to set a valid working directory.",
            )
            return

        # Process file uploads
        uploaded_files = []
        if files:
            logger.info(f"Processing {len(files)} uploaded file(s)")

            uploads_dir = _slack_uploads_dir()
            uploads_dir.mkdir(parents=True, exist_ok=True)
            await deps.db.add_session_dir(channel_id, thread_ts, str(uploads_dir))

            for file_info in files:
                try:
                    file_name = file_info.get("name", "unknown")
                    file_id = file_info["id"]
                    logger.info(f"Processing file: {file_name}")

                    # download_slack_file handles both regular files and snippets
                    # It detects snippets from the full file info and extracts content
                    local_path, metadata = await download_slack_file(
                        client=client,
                        file_id=file_id,
                        slack_bot_token=config.SLACK_BOT_TOKEN,
                        destination_dir=str(uploads_dir),
                        max_size_bytes=config.MAX_FILE_SIZE_MB * 1024 * 1024,
                    )

                    # Track in database
                    uploaded_file = await deps.db.add_uploaded_file(
                        session_id=session.id,
                        slack_file_id=file_id,
                        filename=file_name,
                        local_path=local_path,
                        mimetype=file_info.get("mimetype", ""),
                        size=metadata.get("size", file_info.get("size", 0)),
                    )
                    uploaded_files.append(uploaded_file)
                    logger.info(f"File processed and tracked: {local_path}")

                    # For images, show thumbnail in thread
                    if file_info.get("mimetype", "").startswith("image/"):
                        thumb_url = file_info.get("thumb_360") or file_info.get(
                            "thumb_160"
                        )
                        if thumb_url:
                            await client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=thread_ts
                                or event.get("ts"),  # Use message ts if not in thread
                                text=f"📎 Uploaded: {file_info['name']}",
                                blocks=[
                                    {
                                        "type": "section",
                                        "text": {
                                            "type": "mrkdwn",
                                            "text": f":frame_with_picture: Uploaded image: *{file_info['name']}*",
                                        },
                                    },
                                    {
                                        "type": "image",
                                        "image_url": thumb_url,
                                        "alt_text": file_info["name"],
                                    },
                                ],
                            )

                except FileTooLargeError as e:
                    logger.warning(f"File too large: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ File too large: {file_info['name']} ({e.size_mb:.1f}MB, max: {e.max_mb}MB)",
                    )
                except FileDownloadError as e:
                    logger.error(f"File download failed: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Failed to download file: {file_info['name']} - {str(e)}",
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error processing file {file_info.get('name')}: {e}\n{traceback.format_exc()}"
                    )
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Error processing file: {file_info['name']} - {str(e)}",
                    )

        runtime_mode_directive: Optional[str] = None
        if prompt:
            try:
                prompt, runtime_mode_directive = _extract_single_prompt_mode_directive(prompt)
            except ModeDirectiveError as e:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"Invalid mode directive: {e}",
                    blocks=error_message(f"Invalid mode directive: {e}"),
                )
                return

        queue_plan_prompt = prompt
        if not queue_plan_prompt and uploaded_files:
            queue_plan_prompt = (
                _extract_structured_queue_plan_from_uploaded_files(
                    uploaded_files=uploaded_files,
                    logger=logger,
                )
                or ""
            )

        if queue_plan_prompt:
            structured_message_queued = await _queue_structured_plan_message(
                client=client,
                deps=deps,
                session=session,
                channel_id=channel_id,
                thread_ts=thread_ts,
                prompt=queue_plan_prompt,
                logger=logger,
            )
            if structured_message_queued:
                return

        # Enhance prompt with uploaded file references for normal (non-queue-plan) execution
        if uploaded_files:
            file_refs = "\n".join(
                [f"- {f.filename} (at {f.local_path})" for f in uploaded_files]
            )

            if prompt:
                prompt = f"{prompt}\n\nUploaded files:\n{file_refs}"
            else:
                # No text, only files - provide default prompt
                prompt = f"Please analyze these uploaded files:\n{file_refs}"

        try:
            # Determine which backend to use based on session model
            backend = get_backend_for_model(session.model)
            effective_session = session
            if runtime_mode_directive:
                try:
                    runtime_overrides = resolve_runtime_mode_value(
                        runtime_mode_directive,
                        backend=backend,
                    )
                except ModeDirectiveError as e:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"Invalid mode directive: {e}",
                        blocks=error_message(f"Invalid mode directive: {e}"),
                    )
                    return
                effective_session = _apply_runtime_mode_overrides_to_session(
                    session,
                    runtime_overrides,
                )
            transport = "Claude CLI" if backend == "claude" else "Codex app-server"
            logger.info(
                f"Using backend: {backend} via {transport} (model: {effective_session.model})"
            )

            # Route to appropriate execution path
            if backend == "codex":
                handled_active_turn = (
                    await _route_codex_message_to_active_turn_or_queue(
                        client=client,
                        deps=deps,
                        session=effective_session,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        prompt=prompt,
                        runtime_mode_directive=runtime_mode_directive,
                        logger=logger,
                    )
                )
                if handled_active_turn:
                    return
            elif backend == "claude":
                handled_active_execution = (
                    await _route_claude_message_to_active_execution_or_queue(
                        client=client,
                        deps=deps,
                        session=effective_session,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        prompt=prompt,
                        runtime_mode_directive=runtime_mode_directive,
                        logger=logger,
                    )
                )
                if handled_active_execution:
                    return

            await execute_prompt_with_runtime(
                deps=deps,
                session=effective_session,
                prompt=prompt,
                channel_id=channel_id,
                thread_ts=thread_ts,
                client=client,
                logger=logger,
                user_id=user_id,
                api_with_retry=slack_api_with_retry,
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(
                f"Failed processing message for channel={channel_id} thread_ts={thread_ts}: {e}\n"
                f"{traceback.format_exc()}"
            )
            try:
                await _post_message_processing_error(
                    client=client,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    error_text=str(e),
                )
            except Exception as notify_error:
                logger.error(
                    f"Failed to post message-processing error notice: {notify_error}"
                )
            return

    # Start Socket Mode handler
    handler = AsyncSocketModeHandler(app, config.SLACK_APP_TOKEN)

    # Setup shutdown handler
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info("Starting Slack Claude Code Bot...")
    logger.info(f"Default working directory: {config.DEFAULT_WORKING_DIR}")

    # Start the handler
    await handler.connect_async()
    logger.info("Connected to Slack")
    await _restore_pending_queue_processors(client=app.client, deps=deps, logger=logger)
    await ensure_queue_schedule_dispatcher(
        deps=deps, client=app.client, task_logger=logger
    )

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Cleanup
    await shutdown(claude_executor, codex_executor)
    await handler.close_async()


def run():
    # Check for subcommands (e.g., aislack config ...)
    if len(sys.argv) > 1 and sys.argv[0].endswith(
        ("aislack", "aislack.exe", "ccslack", "ccslack.exe")
    ):
        subcommand = sys.argv[1].lower()
        if subcommand == "config":
            # Forward to config CLI with remaining args
            sys.argv = sys.argv[1:]  # Remove 'aislack' from argv
            from src.cli import run as config_run

            return config_run()

    asyncio.run(main())


if __name__ == "__main__":
    run()
