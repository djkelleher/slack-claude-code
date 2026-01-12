"""Multi-agent workflow command handlers: /task, /tasks, /task-cancel."""

import asyncio
import uuid

from slack_bolt.async_app import AsyncApp

from src.agents.orchestrator import AgentTask, TaskStatus
from src.config import config
from src.utils.formatting import SlackFormatter

from .base import CommandContext, HandlerDependencies, slack_command


def register_agent_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register multi-agent workflow command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/task")
    async def handle_task(ack, command, client, logger):
        """Handle /task <description> command - start multi-agent workflow.

        This handler manages a complex workflow with background execution.
        """
        await ack()

        channel_id = command["channel_id"]
        thread_ts = command.get("thread_ts")
        description = command.get("text", "").strip()

        if not description:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(
                    "Please provide a task description. Usage: /task <description>"
                ),
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Create task
        task_id = str(uuid.uuid4())[:8]
        task = AgentTask(
            task_id=task_id,
            description=description,
            channel_id=channel_id,
            working_directory=session.working_directory,
        )

        # Send initial message
        response = await client.chat_postMessage(
            channel=channel_id,
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":robot_face: Multi-Agent Task Started",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Task ID:* `{task_id}`\n"
                        f"*Status:* :hourglass: Planning...\n\n"
                        f"> {description[:500]}",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Workflow: Planner → Worker → Evaluator",
                        }
                    ],
                },
            ],
        )
        task.message_ts = response["ts"]
        task.slack_thread_ts = response["ts"]

        # Status update callback
        async def on_status_update(updated_task: AgentTask):
            status_emoji = {
                TaskStatus.PLANNING: ":thinking_face: Planning",
                TaskStatus.WORKING: ":hammer_and_wrench: Working",
                TaskStatus.EVALUATING: ":mag: Evaluating",
                TaskStatus.COMPLETED: ":heavy_check_mark: Completed",
                TaskStatus.FAILED: ":x: Failed",
                TaskStatus.CANCELLED: ":no_entry: Cancelled",
            }.get(updated_task.status, ":hourglass: Pending")

            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=task.message_ts,
                    blocks=[
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": ":robot_face: Multi-Agent Task",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Task ID:* `{task_id}`\n"
                                f"*Status:* {status_emoji}\n"
                                f"*Turns:* {updated_task.turn_count}\n\n"
                                f"> {description[:300]}",
                            },
                        },
                    ],
                )
            except Exception as e:
                logger.warning(f"Failed to update task message: {e}")

        # Run workflow in background
        async def run_workflow():
            try:
                result = await deps.orchestrator.execute_workflow(task, on_status_update)

                # Send final result
                if result.success:
                    output = result.work_output or "Task completed successfully."
                    if len(output) > 2500:
                        output = output[:2500] + "\n\n... (output truncated)"

                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=task.slack_thread_ts,
                        blocks=[
                            {
                                "type": "header",
                                "text": {
                                    "type": "plain_text",
                                    "text": ":heavy_check_mark: Task Completed",
                                    "emoji": True,
                                },
                            },
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"*Evaluation:* "
                                    f"{result.eval_result.value if result.eval_result else 'N/A'}\n"
                                    f"*Turns:* {result.total_turns}\n"
                                    f"*Duration:* {result.duration_ms}ms",
                                },
                            },
                            {"type": "divider"},
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": output},
                            },
                        ],
                    )
                else:
                    error = task.error_message or "Unknown error"
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=task.slack_thread_ts,
                        blocks=SlackFormatter.error_message(f"Task failed: {error}"),
                    )

            except Exception as e:
                logger.error(f"Workflow execution error: {e}")
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=task.slack_thread_ts,
                    blocks=SlackFormatter.error_message(f"Workflow error: {e}"),
                )

        # Run workflow in background with proper task tracking
        from src.tasks.manager import TaskManager

        workflow_task = asyncio.create_task(run_workflow())
        await TaskManager.register(
            task_id=f"workflow_{task_id}",
            task=workflow_task,
            channel_id=channel_id,
            task_type="multi_agent_workflow",
            timeout_seconds=3600,  # 1 hour timeout for complex workflows
        )

    @app.command("/tasks")
    @slack_command()
    async def handle_tasks(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /tasks command - list active tasks."""
        active_tasks = deps.orchestrator.get_active_tasks()

        if not active_tasks:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=":clipboard: No active tasks. Start one with `/task <description>`",
            )
            return

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":clipboard: Active Tasks",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        for task in active_tasks:
            status_emoji = {
                TaskStatus.PENDING: ":hourglass:",
                TaskStatus.PLANNING: ":thinking_face:",
                TaskStatus.WORKING: ":hammer_and_wrench:",
                TaskStatus.EVALUATING: ":mag:",
            }.get(task.status, ":grey_question:")

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{task.task_id}* {status_emoji} {task.status.value}\n"
                        f"> {task.description[:100]}...",
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "cancel_task",
                        "value": task.task_id,
                    },
                }
            )

        await ctx.client.chat_postMessage(channel=ctx.channel_id, blocks=blocks)

    @app.command("/task-cancel")
    @slack_command(require_text=True, usage_hint="Usage: /task-cancel <task_id>")
    async def handle_task_cancel(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /task-cancel <id> command."""
        cancelled = await deps.orchestrator.cancel_task(ctx.text)

        if cancelled:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":no_entry: Task `{ctx.text}` cancelled.",
            )
        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Task `{ctx.text}` not found or already completed.",
            )
