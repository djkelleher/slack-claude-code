"""Shared runtime execution orchestration for Slack command/message flows."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from slack_sdk.errors import SlackApiError

from src.handlers.command_router import CommandRouteResult, execute_for_session
from src.handlers.response_delivery import deliver_command_response
from src.question.manager import QuestionManager
from src.utils.formatters.command import error_message
from src.utils.formatters.streaming import processing_fallback_text, processing_message
from src.utils.mode_directives import PlanModeDirective
from src.utils.streaming import StreamingMessageState, create_streaming_callback


@dataclass
class ExecutionDeliveryResult:
    """Result metadata for a fully executed and delivered prompt."""

    route: CommandRouteResult
    command_id: int
    message_ts: str


def streaming_flags_for_session(session: Any) -> tuple[bool, bool]:
    """Return `(smart_concat, terminal_style)` based on selected backend."""
    backend = session.get_backend()
    return backend == "claude", backend == "codex"


async def _cleanup_runtime_state(
    *,
    streaming_state: StreamingMessageState,
    session_id: str,
) -> None:
    """Stop heartbeat and clear pending interactive prompts for a session."""
    await streaming_state.stop_heartbeat()
    await QuestionManager.cancel_for_session(session_id)


async def execute_prompt_with_runtime(
    *,
    deps: Any,
    session: Any,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    client: Any,
    logger: Any,
    user_id: Optional[str] = None,
    api_with_retry: Optional[
        Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]
    ] = None,
    processing_text: Optional[str] = None,
    allow_live_pty: bool = True,
    plan_mode_directive: Optional[PlanModeDirective] = None,
) -> ExecutionDeliveryResult:
    """Execute a prompt through backend router and deliver final Slack output."""
    cmd_history = await deps.db.add_command(session.id, prompt)
    await deps.db.update_command_status(cmd_history.id, "running")

    initial_text = processing_text or processing_fallback_text(prompt)
    response = await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=initial_text,
        blocks=processing_message(prompt),
    )
    message_ts = response["ts"]

    smart_concat, terminal_style = streaming_flags_for_session(session)
    execution_id = str(uuid.uuid4())

    async def on_streaming_error(error_msg: str) -> None:
        """Post streaming-update failures back to the user thread."""
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":warning: {error_msg}",
            )
        except Exception as e:
            logger.error(f"Failed to post streaming error notification: {e}")

    def _create_streaming_state(
        message_timestamp: str, display_prompt: str
    ) -> StreamingMessageState:
        state = StreamingMessageState(
            channel_id=channel_id,
            message_ts=message_timestamp,
            prompt=display_prompt,
            client=client,
            logger=logger,
            track_tools=True,
            smart_concat=smart_concat,
            terminal_style=terminal_style,
            db_session_id=session.id,
            on_error=on_streaming_error,
        )
        state.start_heartbeat()
        return state

    streaming_state = _create_streaming_state(message_ts, prompt)
    on_chunk = create_streaming_callback(streaming_state)

    async def on_interaction_resumed() -> Any:
        nonlocal message_ts, streaming_state
        await streaming_state.stop_heartbeat()

        continue_response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Continuing...",
            blocks=processing_message("Continuing after Slack interaction..."),
        )
        message_ts = continue_response["ts"]
        streaming_state = _create_streaming_state(message_ts, prompt)
        return create_streaming_callback(streaming_state)

    async def on_plan_approved() -> Any:
        nonlocal message_ts, streaming_state
        await streaming_state.finalize()

        exec_response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Plan approved - executing...",
            blocks=processing_message(
                ":white_check_mark: *Plan approved!* Executing implementation..."
            ),
        )
        message_ts = exec_response["ts"]
        streaming_state = _create_streaming_state(message_ts, "[Plan Execution]")
        return create_streaming_callback(streaming_state)

    try:
        route = await execute_for_session(
            deps=deps,
            session=session,
            prompt=prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            execution_id=execution_id,
            on_chunk=on_chunk,
            slack_client=client,
            user_id=user_id,
            logger=logger,
            on_plan_approved=on_plan_approved,
            on_interaction_resumed=on_interaction_resumed,
            allow_live_pty=allow_live_pty,
            plan_mode_directive=plan_mode_directive,
        )
        result = route.result

        if result.success:
            await deps.db.update_command_status(
                cmd_history.id, "completed", result.output
            )
        else:
            await deps.db.update_command_status(
                cmd_history.id, "failed", result.output, result.error
            )

        await streaming_state.stop_heartbeat()

        output = result.output or result.error or "No output"
        await deliver_command_response(
            client=client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            prompt=prompt,
            output=output,
            command_id=cmd_history.id,
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            is_error=not result.success,
            logger=logger,
            db=deps.db,
            detailed_output=result.detailed_output,
            post_detail_button=True,
            notify_on_snippet_failure=True,
            api_with_retry=api_with_retry,
            terminal_style=route.backend == "codex",
            working_directory=session.working_directory,
            upload_git_diff=True,
            git_tool_events=result.git_tool_events,
            upload_git_activity=True,
        )
        return ExecutionDeliveryResult(
            route=route, command_id=cmd_history.id, message_ts=message_ts
        )

    except asyncio.CancelledError:
        logger.info("Command execution was cancelled")
        await _cleanup_runtime_state(
            streaming_state=streaming_state, session_id=str(session.id)
        )
        await deps.db.update_command_status(
            cmd_history.id, "cancelled", error_message="Cancelled"
        )
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="Command cancelled",
            blocks=error_message("Command was cancelled"),
        )
        raise
    except SlackApiError as e:
        logger.error(f"Slack API error executing command: {e}")
        await _cleanup_runtime_state(
            streaming_state=streaming_state, session_id=str(session.id)
        )
        await deps.db.update_command_status(
            cmd_history.id, "failed", error_message=str(e)
        )
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":x: Slack API Error: {str(e)[:200]}",
                blocks=error_message(f"Slack API Error: {str(e)}"),
            )
        except Exception as notify_error:
            logger.error(f"Failed to post Slack API error notification: {notify_error}")
        raise
    except (OSError, IOError) as e:
        logger.error(f"I/O error executing command: {e}")
        await _cleanup_runtime_state(
            streaming_state=streaming_state, session_id=str(session.id)
        )
        await deps.db.update_command_status(
            cmd_history.id, "failed", error_message=str(e)
        )
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"I/O Error: {str(e)}",
            blocks=error_message(f"I/O Error: {str(e)}"),
        )
        raise
    except Exception as e:
        logger.error(f"Unexpected error executing command: {type(e).__name__}: {e}")
        await _cleanup_runtime_state(
            streaming_state=streaming_state, session_id=str(session.id)
        )
        await deps.db.update_command_status(
            cmd_history.id, "failed", error_message=str(e)
        )
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"Error: {str(e)}",
            blocks=error_message(str(e)),
        )
        raise
