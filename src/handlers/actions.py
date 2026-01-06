"""Interactive component action handlers."""

import asyncio
import uuid

from slack_bolt.async_app import AsyncApp

from src.approval import PermissionManager, build_approval_result_blocks
from src.approval.plan_manager import PlanApprovalManager
from src.approval.slack_ui import build_plan_result_blocks
from src.config import config
from src.pty import PTYSessionPool
from src.utils.formatting import SlackFormatter

from .base import HandlerDependencies


# Reference to message update throttle for convenience
_msg_throttle = config.timeouts.slack.message_update_throttle


def register_actions(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register all interactive component handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.action("rerun_command")
    async def handle_rerun(ack, action, body, client, logger):
        """Handle rerun button click."""
        await ack()

        try:
            channel_id = body["channel"]["id"]
            command_id = int(action["value"])
            # Get thread_ts from the message context if available
            thread_ts = body.get("message", {}).get("thread_ts")
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action data: {e}")
            return

        # Get original command
        cmd = await deps.db.get_command_by_id(command_id)
        if not cmd:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text="Command not found.",
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Create new command history entry
        new_cmd = await deps.db.add_command(session.id, cmd.command)
        await deps.db.update_command_status(new_cmd.id, "running")

        # Send processing message
        response = await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.processing_message(cmd.command),
        )
        message_ts = response["ts"]

        # Execute command
        execution_id = str(uuid.uuid4())

        accumulated_output = ""
        last_update_time = 0

        async def on_chunk(msg):
            nonlocal accumulated_output, last_update_time

            if msg.type == "assistant" and msg.content:
                # Limit accumulated output to prevent memory issues
                if len(accumulated_output) < config.timeouts.streaming.max_accumulated_size:
                    accumulated_output += msg.content

                current_time = asyncio.get_running_loop().time()
                if current_time - last_update_time > _msg_throttle:
                    last_update_time = current_time
                    try:
                        await client.chat_update(
                            channel=channel_id,
                            ts=message_ts,
                            blocks=SlackFormatter.streaming_update(
                                cmd.command, accumulated_output
                            ),
                        )
                    except Exception as e:
                        logger.warning(f"Failed to update message: {e}")

        try:
            result = await deps.executor.execute(
                prompt=cmd.command,
                working_directory=session.working_directory,
                session_id=session.claude_session_id,
                execution_id=execution_id,
                on_chunk=on_chunk,
                db_session_id=session.id,  # Smart context tracking
            )

            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, None, result.session_id)

            if result.success:
                await deps.db.update_command_status(
                    new_cmd.id, "completed", result.output
                )
            else:
                await deps.db.update_command_status(
                    new_cmd.id, "failed", result.output, result.error
                )

            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=SlackFormatter.command_response(
                    prompt=cmd.command,
                    output=result.output or result.error or "No output",
                    command_id=new_cmd.id,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                ),
            )

        except Exception as e:
            logger.error(f"Error rerunning command: {e}")
            await deps.db.update_command_status(
                new_cmd.id, "failed", error_message=str(e)
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=SlackFormatter.error_message(str(e)),
            )

    @app.action("view_output")
    async def handle_view_output(ack, action, body, client, logger):
        """Handle view output button click."""
        await ack()

        try:
            command_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action value: {e}")
            return

        cmd = await deps.db.get_command_by_id(command_id)

        if not cmd:
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Command Not Found"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "This command could not be found.",
                            },
                        }
                    ],
                },
            )
            return

        output = cmd.output or "No output"
        # Truncate for modal (max ~3000 chars)
        if len(output) > 2900:
            output = output[:2900] + "\n\n... (output truncated)"

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": f"Command #{cmd.id}"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Status:* {cmd.status} | "
                                f"*Created:* {cmd.created_at.strftime('%Y-%m-%d %H:%M')}",
                            }
                        ],
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Prompt:*\n> {cmd.command}",
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Output:*\n{output}"},
                    },
                ],
            },
        )

    @app.action("view_parallel_results")
    async def handle_view_parallel_results(ack, action, body, client, logger):
        """Handle view parallel results button click."""
        await ack()

        try:
            job_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action value: {e}")
            return

        job = await deps.db.get_parallel_job(job_id)

        if not job:
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Job Not Found"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "This job could not be found.",
                            },
                        }
                    ],
                },
            )
            return

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Parallel Job #{job.id} Results",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        for result in job.results:
            terminal_num = result.get("terminal", "?")
            output = result.get("output", result.get("error", "No output"))
            if len(output) > 500:
                output = output[:500] + "\n... (truncated)"

            status = ":white_check_mark:" if result.get("success") else ":x:"

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Terminal {terminal_num}* {status}\n```{output}```",
                    },
                }
            )

        if job.aggregation_output:
            agg_output = job.aggregation_output
            if len(agg_output) > 800:
                agg_output = agg_output[:800] + "\n... (truncated)"

            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Aggregated Result:*\n{agg_output}",
                    },
                }
            )

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Parallel Results"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": blocks[:50],  # Modal block limit
            },
        )

    @app.action("cancel_job")
    async def handle_cancel_job(ack, action, body, client, logger):
        """Handle cancel job button click."""
        await ack()

        try:
            channel_id = body["channel"]["id"]
            job_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action data: {e}")
            return

        cancelled = await deps.db.cancel_job(job_id)

        if cancelled:
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_entry: Job #{job_id} cancelled.",
            )

            # Update the job status message if we have it
            job = await deps.db.get_parallel_job(job_id)
            if job and job.message_ts:
                try:
                    if job.job_type == "parallel_analysis":
                        blocks = SlackFormatter.parallel_job_status(job)
                    else:
                        blocks = SlackFormatter.sequential_job_status(job)

                    await client.chat_update(
                        channel=channel_id,
                        ts=job.message_ts,
                        blocks=blocks,
                    )
                except Exception as e:
                    logger.warning(f"Failed to update job message: {e}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text=f"Job #{job_id} not found or already completed.",
            )

    # -------------------------------------------------------------------------
    # Permission approval handlers
    # -------------------------------------------------------------------------

    @app.action("approve_tool")
    async def handle_approve_tool(ack, action, body, client, logger):
        """Handle tool approval button click."""
        await ack()

        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        approval_id = action["value"]

        # Resolve the approval
        resolved = await PermissionManager.resolve(approval_id, approved=True)

        if resolved:
            # Update the message to show approved status
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=build_approval_result_blocks(
                        approval_id=approval_id,
                        tool_name=resolved.tool_name,
                        approved=True,
                        user_id=user_id,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to update approval message: {e}")

            logger.info(f"Tool {resolved.tool_name} approved by {user_id}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Approval request `{approval_id}` not found or already resolved.",
            )

    @app.action("deny_tool")
    async def handle_deny_tool(ack, action, body, client, logger):
        """Handle tool denial button click."""
        await ack()

        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        approval_id = action["value"]

        # Resolve the approval (denied)
        resolved = await PermissionManager.resolve(approval_id, approved=False)

        if resolved:
            # Update the message to show denied status
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=build_approval_result_blocks(
                        approval_id=approval_id,
                        tool_name=resolved.tool_name,
                        approved=False,
                        user_id=user_id,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to update denial message: {e}")

            logger.info(f"Tool {resolved.tool_name} denied by {user_id}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Approval request `{approval_id}` not found or already resolved.",
            )

    @app.action("approve_plan")
    async def handle_approve_plan(ack, action, body, client, logger):
        """Handle plan approval button click."""
        await ack()

        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        approval_id = action["value"]

        # Resolve the approval
        resolved = await PlanApprovalManager.resolve(
            approval_id=approval_id,
            approved=True,
            resolved_by=user_id,
        )

        if resolved:
            # Update the message to show approved status
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=build_plan_result_blocks(
                        approval_id=approval_id,
                        approved=True,
                        user_id=user_id,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to update plan approval message: {e}")

            logger.info(f"Plan {approval_id} approved by {user_id}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Plan approval request `{approval_id}` not found or already resolved.",
            )

    @app.action("reject_plan")
    async def handle_reject_plan(ack, action, body, client, logger):
        """Handle plan rejection button click."""
        await ack()

        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        approval_id = action["value"]

        # Resolve the approval (rejected)
        resolved = await PlanApprovalManager.resolve(
            approval_id=approval_id,
            approved=False,
            resolved_by=user_id,
        )

        if resolved:
            # Update the message to show rejected status
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=build_plan_result_blocks(
                        approval_id=approval_id,
                        approved=False,
                        user_id=user_id,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to update plan rejection message: {e}")

            logger.info(f"Plan {approval_id} rejected by {user_id}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Plan approval request `{approval_id}` not found or already resolved.",
            )

    # -------------------------------------------------------------------------
    # Task management handlers
    # -------------------------------------------------------------------------

    @app.action("cancel_task")
    async def handle_cancel_task(ack, action, body, client, logger):
        """Handle cancel task button click."""
        await ack()

        channel_id = body["channel"]["id"]
        task_id = action["value"]

        # Use the shared orchestrator from dependencies
        cancelled = await deps.orchestrator.cancel_task(task_id)

        if cancelled:
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_entry: Task `{task_id}` cancelled.",
            )
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text=f"Task `{task_id}` not found or already completed.",
            )

    # -------------------------------------------------------------------------
    # PTY session handlers
    # -------------------------------------------------------------------------

    @app.action("restart_pty")
    async def handle_restart_pty(ack, action, body, client, logger):
        """Handle PTY session restart button click."""
        await ack()

        channel_id = action["value"]
        user_id = body["user"]["id"]

        # Remove existing session
        await PTYSessionPool.remove(channel_id)

        await client.chat_postMessage(
            channel=channel_id,
            text=":arrows_counterclockwise: PTY session restarted. "
            "A new session will be created on your next command.",
        )

        logger.info(f"PTY session for {channel_id} restarted by {user_id}")
