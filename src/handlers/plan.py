"""Plan mode command handler: /plan."""

import asyncio
import logging
import uuid
from typing import Optional

from slack_bolt.async_app import AsyncApp

from ..approval.plan_manager import PlanApprovalManager
from ..approval.slack_ui import build_plan_approval_blocks
from ..config import config
from ..utils.formatters.plan import (
    plan_execution_complete,
    plan_execution_update,
    plan_processing_message,
    plan_ready_message,
)
from ..utils.formatting import SlackFormatter
from .base import CommandContext, HandlerDependencies, slack_command

logger = logging.getLogger(__name__)


def register_plan_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register plan command handler.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/plan")
    @slack_command(require_text=True, usage_hint="Usage: /plan <prompt>")
    async def handle_plan(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /plan <prompt> command - plan mode execution."""
        prompt = ctx.text

        # Get or create session
        session = await deps.db.get_or_create_session(
            ctx.channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create command history entry for planning phase
        cmd_history = await deps.db.add_command(session.id, f"[PLAN] {prompt}")
        await deps.db.update_command_status(cmd_history.id, "running")

        # Send initial processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Creating plan: {prompt[:100]}...",
            blocks=plan_processing_message(prompt),
        )
        message_ts = response["ts"]

        # Phase 1: Planning
        # Execute with --permission-mode plan
        planning_result = await execute_planning_phase(
            deps=deps,
            prompt=prompt,
            session=session,
            ctx=ctx,
            message_ts=message_ts,
        )

        if not planning_result["success"]:
            # Planning failed
            await deps.db.update_command_status(
                cmd_history.id, "failed", "", planning_result.get("error", "Planning failed")
            )
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text="Planning failed",
                blocks=SlackFormatter.error_message(
                    planning_result.get("error", "Planning failed")
                ),
            )
            return

        # Planning succeeded - show approval UI
        plan_content = planning_result["plan_content"]
        plan_session_id = planning_result["session_id"]

        # Update planning command as completed
        await deps.db.update_command_status(cmd_history.id, "completed", plan_content)
        if plan_session_id:
            await deps.db.update_session_claude_id(ctx.channel_id, plan_session_id)

        # Update message to show plan is ready
        approval_id = str(uuid.uuid4())[:8]
        await ctx.client.chat_update(
            channel=ctx.channel_id,
            ts=message_ts,
            text="Plan ready for review",
            blocks=plan_ready_message(prompt, plan_content[:500], approval_id),
        )

        # Post plan approval message
        plan_blocks = build_plan_approval_blocks(
            approval_id=approval_id,
            plan_content=plan_content,
            session_id=ctx.channel_id,
        )
        plan_message = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text="Review plan",
            blocks=plan_blocks,
        )

        # Upload full plan as file if it's large
        if len(plan_content) > 2000:
            try:
                await ctx.client.files_upload_v2(
                    channel=ctx.channel_id,
                    content=plan_content,
                    filename=f"plan_{approval_id}.md",
                    title="Full Implementation Plan",
                    initial_comment="📋 Complete implementation plan",
                    filetype="text",
                )
            except Exception as e:
                logger.warning(f"Failed to upload plan file: {e}")

        # Wait for approval
        try:
            approved = await PlanApprovalManager.request_approval(
                session_id=ctx.channel_id,
                channel_id=ctx.channel_id,
                plan_content=plan_content,
                claude_session_id=plan_session_id,
                prompt=prompt,
                user_id=ctx.user_id,
                slack_client=ctx.client,
                timeout=config.timeouts.execution.plan_approval,
            )
        except Exception as e:
            logger.error(f"Error during plan approval: {e}")
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"⚠️ Plan approval error: {e}",
            )
            return

        if not approved:
            # Plan rejected or timed out
            logger.info(f"Plan {approval_id} rejected or timed out")
            return

        # Phase 2: Execution
        # Plan approved - execute with --resume
        exec_cmd_history = await deps.db.add_command(session.id, f"[EXEC] {prompt}")
        await deps.db.update_command_status(exec_cmd_history.id, "running")

        # Post execution message
        exec_response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Executing plan: {prompt[:100]}...",
            blocks=plan_execution_update(prompt, "Starting execution..."),
        )
        exec_message_ts = exec_response["ts"]

        # Execute with streaming updates
        accumulated_output = ""
        last_update_time = 0
        execution_id = str(uuid.uuid4())

        async def on_chunk(msg):
            nonlocal accumulated_output, last_update_time

            if msg.type == "assistant" and msg.content:
                # Limit accumulated output to prevent memory issues
                if len(accumulated_output) < config.timeouts.streaming.max_accumulated_size:
                    accumulated_output += msg.content

                # Rate limit updates to avoid Slack API limits
                current_time = asyncio.get_running_loop().time()
                if current_time - last_update_time > config.timeouts.slack.message_update_throttle:
                    last_update_time = current_time
                    try:
                        await ctx.client.chat_update(
                            channel=ctx.channel_id,
                            ts=exec_message_ts,
                            text=accumulated_output[:100] + "..." if len(accumulated_output) > 100 else accumulated_output,
                            blocks=plan_execution_update(prompt, accumulated_output),
                        )
                    except Exception as e:
                        ctx.logger.warning(f"Failed to update execution message: {e}")

        try:
            result = await deps.executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=ctx.channel_id,
                resume_session_id=plan_session_id,  # Resume from planning session
                execution_id=execution_id,
                on_chunk=on_chunk,
            )

            # Update session with new Claude session ID
            if result.session_id:
                await deps.db.update_session_claude_id(ctx.channel_id, result.session_id)

            # Update command history
            if result.success:
                await deps.db.update_command_status(
                    exec_cmd_history.id, "completed", result.output
                )
            else:
                await deps.db.update_command_status(
                    exec_cmd_history.id, "failed", result.output, result.error
                )

            # Send final response
            output = result.output or result.error or "No output"

            if SlackFormatter.should_attach_file(output):
                # Large response - attach as file
                blocks = plan_execution_complete(
                    prompt=prompt,
                    output=output[:500] + "\n\n_... (see attached files for full output)_",
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    command_id=exec_cmd_history.id,
                )
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=exec_message_ts,
                    text=output[:100] + "..." if len(output) > 100 else output,
                    blocks=blocks,
                )
                # Upload files as separate messages
                try:
                    # Upload summary file
                    await ctx.client.files_upload_v2(
                        channel=ctx.channel_id,
                        content=output,
                        filename=f"plan_execution_{exec_cmd_history.id}.txt",
                        title="Plan Execution Summary",
                        initial_comment="📄 Execution summary",
                        filetype="text",
                    )
                    # Upload full detailed output file if available
                    if result.detailed_output and result.detailed_output != output:
                        await ctx.client.files_upload_v2(
                            channel=ctx.channel_id,
                            content=result.detailed_output,
                            filename=f"plan_execution_detailed_{exec_cmd_history.id}.txt",
                            title="Plan Execution Details",
                            initial_comment="📋 Complete execution with tool use and results",
                            filetype="text",
                        )
                except Exception as upload_error:
                    ctx.logger.error(f"Failed to upload execution file: {upload_error}")
            else:
                # Normal response
                blocks = plan_execution_complete(
                    prompt=prompt,
                    output=output,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    command_id=exec_cmd_history.id,
                )
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=exec_message_ts,
                    text=output[:100] + "..." if len(output) > 100 else output,
                    blocks=blocks,
                )

        except Exception as e:
            ctx.logger.error(f"Error during plan execution: {e}")
            await deps.db.update_command_status(
                exec_cmd_history.id, "failed", "", str(e)
            )
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=exec_message_ts,
                text="Execution failed",
                blocks=SlackFormatter.error_message(str(e)),
            )


async def execute_planning_phase(
    deps: HandlerDependencies,
    prompt: str,
    session,
    ctx: CommandContext,
    message_ts: str,
) -> dict:
    """Execute the planning phase with --permission-mode plan.

    Returns
    -------
    dict
        Result dictionary with keys:
        - success: bool
        - plan_content: str (if successful)
        - session_id: str (if successful)
        - error: str (if failed)
    """
    # Build command for planning phase
    # Note: We need to modify the executor to support --permission-mode plan
    # For now, this is a placeholder that uses the standard executor
    # The actual implementation will require executor changes

    execution_id = str(uuid.uuid4())
    accumulated_output = ""
    last_update_time = 0

    async def on_chunk(msg):
        nonlocal accumulated_output, last_update_time

        if msg.type == "assistant" and msg.content:
            # Accumulate plan content
            if len(accumulated_output) < config.timeouts.streaming.max_accumulated_size:
                accumulated_output += msg.content

            # Rate limit updates
            current_time = asyncio.get_running_loop().time()
            if current_time - last_update_time > config.timeouts.slack.message_update_throttle:
                last_update_time = current_time
                try:
                    await ctx.client.chat_update(
                        channel=ctx.channel_id,
                        ts=message_ts,
                        text=accumulated_output[:100] + "..." if len(accumulated_output) > 100 else accumulated_output,
                        blocks=plan_processing_message(prompt),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update planning message: {e}")

    try:
        # Execute with plan mode enabled
        result = await deps.executor.execute(
            prompt=prompt,
            working_directory=session.working_directory,
            session_id=ctx.channel_id,
            resume_session_id=session.claude_session_id,
            execution_id=execution_id,
            on_chunk=on_chunk,
            plan_mode=True,  # Enable plan mode for planning phase
        )

        if result.success:
            return {
                "success": True,
                "plan_content": result.output or accumulated_output,
                "session_id": result.session_id,
            }
        else:
            return {
                "success": False,
                "error": result.error or "Planning failed",
            }

    except Exception as e:
        logger.error(f"Error during planning phase: {e}")
        return {
            "success": False,
            "error": str(e),
        }
