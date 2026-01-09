"""Interactive component action handlers."""

import asyncio
import json
import re
import uuid

from slack_bolt.async_app import AsyncApp

from src.approval.handler import PermissionManager
from src.approval.slack_ui import build_approval_result_blocks
from src.approval.plan_manager import PlanApprovalManager
from src.approval.slack_ui import build_plan_result_blocks
from src.claude.streaming import ToolActivity
from src.config import config
from src.pty.pool import PTYSessionPool
from src.utils.formatters.tool_blocks import format_tool_detail_blocks
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
                session_id=channel_id,
                resume_session_id=session.claude_session_id,
                execution_id=execution_id,
                on_chunk=on_chunk,
                permission_mode=session.permission_mode,
                db_session_id=session.id,  # Smart context tracking
            )

            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

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

            status = ":heavy_check_mark:" if result.get("success") else ":x:"

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

    # -------------------------------------------------------------------------
    # Tool detail handlers
    # -------------------------------------------------------------------------

    @app.action("view_tool_detail")
    async def handle_view_tool_detail(ack, action, body, client, logger):
        """Handle view tool detail button click.

        Opens a modal with full tool input/output details.
        """
        await ack()

        try:
            tool_data = json.loads(action["value"])
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid tool data: {e}")
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Error"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Could not load tool details.",
                            },
                        }
                    ],
                },
            )
            return

        # Reconstruct ToolActivity from the serialized data
        tool = ToolActivity(
            id=tool_data.get("id", "unknown"),
            name=tool_data.get("name", "unknown"),
            input=tool_data.get("input", {}),
            input_summary=tool_data.get("input_summary", ""),
            result=tool_data.get("result"),
            full_result=tool_data.get("full_result"),
            is_error=tool_data.get("is_error", False),
            duration_ms=tool_data.get("duration_ms"),
        )

        # Get formatted blocks
        detail_blocks = format_tool_detail_blocks(tool)

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": f"Tool: {tool.name}"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": detail_blocks[:50],  # Modal block limit
            },
        )

    # -------------------------------------------------------------------------
    # Question handlers (AskUserQuestion tool)
    # -------------------------------------------------------------------------

    @app.action("question_custom_answer")
    async def handle_question_custom_answer(ack, action, body, client, logger):
        """Handle custom answer button - open modal for text input."""
        await ack()

        from src.question import QuestionManager, build_custom_answer_modal

        question_id = action["value"]

        # Check if question still exists
        pending = QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Open modal for custom answer
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=build_custom_answer_modal(question_id),
        )

    # Register handlers for question option buttons (question_select_*)
    # We use a regex pattern to match all question select actions
    @app.action(re.compile(r"^question_select_\d+_\d+$"))
    async def handle_question_select(ack, action, body, client, logger):
        """Handle single-select question button click."""
        await ack()

        from src.question import QuestionManager, build_question_result_blocks

        try:
            data = json.loads(action["value"])
            question_id = data["q"]
            question_index = data["i"]
            selected_label = data["l"]
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid question action data: {e}")
            return

        pending = QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Set the answer for this question
        QuestionManager.set_answer(question_id, question_index, [selected_label])

        # Check if all questions are answered
        if QuestionManager.is_complete(question_id):
            # Resolve the question
            resolved = await QuestionManager.resolve(question_id)
            if resolved:
                user_id = body["user"]["id"]
                channel_id = body["channel"]["id"]
                message_ts = body["message"]["ts"]

                # Update the message to show answered state
                try:
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        blocks=build_question_result_blocks(resolved, user_id),
                        text="Question answered",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update question message: {e}")

                logger.info(f"Question {question_id} answered by {user_id}: {resolved.answers}")

    @app.action(re.compile(r"^question_multiselect_\d+$"))
    async def handle_question_multiselect(ack, action, body, client, logger):
        """Handle multi-select checkbox change."""
        await ack()

        from src.question import QuestionManager

        # Extract question info from block_id
        block_id = action.get("block_id", "")
        # Format: question_checkbox_{question_id}_{question_index}
        parts = block_id.split("_")
        if len(parts) < 4:
            logger.error(f"Invalid checkbox block_id: {block_id}")
            return

        question_id = parts[2]
        question_index = int(parts[3])

        # Get selected options
        selected_options = action.get("selected_options", [])
        selected_labels = [opt.get("value", "") for opt in selected_options]

        pending = QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Set the answer for this question
        QuestionManager.set_answer(question_id, question_index, selected_labels)

        # For multi-select, we need a submit button - checkboxes alone don't trigger completion
        # The user needs to click a "Submit" button after selecting checkboxes
        # This is handled separately

    @app.view("question_custom_submit")
    async def handle_question_custom_submit(ack, body, client, view, logger):
        """Handle custom answer modal submission."""
        await ack()

        from src.question import QuestionManager, build_question_result_blocks

        question_id = view["private_metadata"]
        custom_answer = view["state"]["values"]["custom_answer_block"]["custom_answer_input"]["value"]

        pending = QuestionManager.get_pending(question_id)
        if not pending:
            # Question already resolved or timed out
            return

        # Set custom answer for all questions (treat as single combined answer)
        for i in range(len(pending.questions)):
            QuestionManager.set_answer(question_id, i, [custom_answer])

        # Resolve the question
        resolved = await QuestionManager.resolve(question_id)
        if resolved and resolved.message_ts:
            user_id = body["user"]["id"]

            # Update the original message
            try:
                await client.chat_update(
                    channel=resolved.channel_id,
                    ts=resolved.message_ts,
                    blocks=build_question_result_blocks(resolved, user_id),
                    text="Question answered",
                )
            except Exception as e:
                logger.warning(f"Failed to update question message: {e}")

            logger.info(f"Question {question_id} custom answered by {user_id}: {custom_answer[:50]}...")
