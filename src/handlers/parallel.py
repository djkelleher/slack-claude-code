"""Parallel and sequential execution command handlers: /g, /s, /l, /st, /cc."""

import asyncio
import logging

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter
from src.utils.validators import parse_parallel_args, parse_loop_args, validate_json_commands

from .base import CommandContext, HandlerDependencies, slack_command

logger = logging.getLogger(__name__)

# Track background tasks to prevent orphaned coroutines
_background_tasks: set[asyncio.Task] = set()

# Maximum concurrent jobs per channel to prevent resource exhaustion
MAX_CONCURRENT_JOBS_PER_CHANNEL = 3


def _create_tracked_task(coro, task_logger=None) -> asyncio.Task:
    """Create a background task with proper tracking and error handling.

    Prevents orphaned tasks by storing references and logging exceptions.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def done_callback(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                log = task_logger or logger
                log.error(f"Background task failed: {exc}", exc_info=exc)

    task.add_done_callback(done_callback)
    return task


def register_parallel_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register parallel and sequential execution command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/g")
    async def handle_gather(ack, command, client, logger):
        """Handle /g <n> <prompt> command - run in N terminals, then aggregate.

        This handler launches a background task for execution.
        """
        await ack()

        channel_id = command["channel_id"]
        text = command.get("text", "").strip()

        valid, result = parse_parallel_args(text)
        if not valid:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(result),
            )
            return

        n, prompt = result

        # Rate limiting: check active jobs for this channel
        active_jobs = await deps.db.get_active_jobs(channel_id)
        if len(active_jobs) >= MAX_CONCURRENT_JOBS_PER_CHANNEL:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(
                    f"Maximum {MAX_CONCURRENT_JOBS_PER_CHANNEL} concurrent jobs allowed. "
                    "Wait for existing jobs to complete or cancel them with /cc."
                ),
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create parallel job
        job = await deps.db.create_parallel_job(
            session_id=session.id,
            channel_id=channel_id,
            job_type="parallel_analysis",
            config={"n_instances": n, "prompt": prompt},
        )

        # Send initial status message
        response = await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.parallel_job_status(job),
        )
        await deps.db.update_parallel_job(job.id, message_ts=response["ts"])

        # Run parallel execution in background
        _create_tracked_task(
            _run_parallel_execution(
                job_id=job.id,
                n=n,
                prompt=prompt,
                working_directory=session.working_directory,
                channel_id=channel_id,
                message_ts=response["ts"],
                deps=deps,
                client=client,
                logger=logger,
            ),
            task_logger=logger,
        )

    @app.command("/s")
    async def handle_sequence(ack, command, client, logger):
        """Handle /s <json_array> command - run commands sequentially.

        This handler launches a background task for execution.
        """
        await ack()

        channel_id = command["channel_id"]
        text = command.get("text", "").strip()

        valid, result = validate_json_commands(text)
        if not valid:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(result),
            )
            return

        commands_list = result

        # Rate limiting: check active jobs for this channel
        active_jobs = await deps.db.get_active_jobs(channel_id)
        if len(active_jobs) >= MAX_CONCURRENT_JOBS_PER_CHANNEL:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(
                    f"Maximum {MAX_CONCURRENT_JOBS_PER_CHANNEL} concurrent jobs allowed. "
                    "Wait for existing jobs to complete or cancel them with /cc."
                ),
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create sequential job (loop_count = 1)
        job = await deps.db.create_parallel_job(
            session_id=session.id,
            channel_id=channel_id,
            job_type="sequential_loop",
            config={"commands": commands_list, "loop_count": 1},
        )

        # Send initial status message
        response = await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.sequential_job_status(job),
        )
        await deps.db.update_parallel_job(job.id, message_ts=response["ts"])

        # Run sequential execution in background
        _create_tracked_task(
            _run_sequential_execution(
                job_id=job.id,
                commands=commands_list,
                loop_count=1,
                working_directory=session.working_directory,
                channel_id=channel_id,
                message_ts=response["ts"],
                deps=deps,
                client=client,
                logger=logger,
            ),
            task_logger=logger,
        )

    @app.command("/l")
    async def handle_loop(ack, command, client, logger):
        """Handle /l <n> <json_array> command - run commands N times.

        This handler launches a background task for execution.
        """
        await ack()

        channel_id = command["channel_id"]
        text = command.get("text", "").strip()

        valid, result = parse_loop_args(text)
        if not valid:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(result),
            )
            return

        loop_count, commands_list = result

        # Rate limiting: check active jobs for this channel
        active_jobs = await deps.db.get_active_jobs(channel_id)
        if len(active_jobs) >= MAX_CONCURRENT_JOBS_PER_CHANNEL:
            await client.chat_postMessage(
                channel=channel_id,
                blocks=SlackFormatter.error_message(
                    f"Maximum {MAX_CONCURRENT_JOBS_PER_CHANNEL} concurrent jobs allowed. "
                    "Wait for existing jobs to complete or cancel them with /cc."
                ),
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create sequential job with loop
        job = await deps.db.create_parallel_job(
            session_id=session.id,
            channel_id=channel_id,
            job_type="sequential_loop",
            config={"commands": commands_list, "loop_count": loop_count},
        )

        # Send initial status message
        response = await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.sequential_job_status(job),
        )
        await deps.db.update_parallel_job(job.id, message_ts=response["ts"])

        # Run sequential execution in background
        _create_tracked_task(
            _run_sequential_execution(
                job_id=job.id,
                commands=commands_list,
                loop_count=loop_count,
                working_directory=session.working_directory,
                channel_id=channel_id,
                message_ts=response["ts"],
                deps=deps,
                client=client,
                logger=logger,
            ),
            task_logger=logger,
        )

    @app.command("/st")
    @slack_command()
    async def handle_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /st command - show active jobs."""
        jobs = await deps.db.get_active_jobs(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.job_status_list(jobs),
        )

    @app.command("/cc")
    @slack_command()
    async def handle_cancel(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cc [job_id] command - cancel jobs."""
        if ctx.text:
            # Cancel specific job
            try:
                job_id = int(ctx.text)
                cancelled = await deps.db.cancel_job(job_id)
                if cancelled:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f":no_entry: Job #{job_id} cancelled.",
                    )
                else:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"Job #{job_id} not found or already completed.",
                    )
            except ValueError:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(
                        "Invalid job ID. Usage: /cc [job_id]"
                    ),
                )
        else:
            # Cancel all active jobs in channel
            jobs = await deps.db.get_active_jobs(ctx.channel_id)
            cancelled_count = 0
            for job in jobs:
                if await deps.db.cancel_job(job.id):
                    cancelled_count += 1

            # Also cancel any active executions
            executor_cancelled = await deps.executor.cancel_all()

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":no_entry: Cancelled {cancelled_count} job(s) and "
                f"{executor_cancelled} active execution(s).",
            )


async def _run_parallel_execution(
    job_id: int,
    n: int,
    prompt: str,
    working_directory: str,
    channel_id: str,
    message_ts: str,
    deps: HandlerDependencies,
    client,
    logger,
) -> None:
    """Run parallel execution and aggregate results.

    Each parallel terminal gets its own independent session.

    Parameters
    ----------
    job_id : int
        The database job ID.
    n : int
        Number of parallel executions.
    prompt : str
        The prompt to execute in each terminal.
    working_directory : str
        Working directory for executions.
    channel_id : str
        Slack channel ID for updates.
    message_ts : str
        Timestamp of the status message to update.
    deps : HandlerDependencies
        Handler dependencies.
    client
        Slack client for API calls.
    logger
        Logger instance.
    """
    cancel_event = asyncio.Event()
    execution_tasks: list[asyncio.Task] = []

    async def check_cancelled() -> None:
        """Periodically check if job was cancelled and cancel running tasks."""
        while not cancel_event.is_set():
            try:
                job = await deps.db.get_parallel_job(job_id)
                if job.status == "cancelled":
                    cancel_event.set()
                    # Cancel all running execution tasks
                    for task in execution_tasks:
                        if not task.done():
                            task.cancel()
                    return
            except Exception as e:
                logger.warning(f"Error checking cancellation: {e}")
            await asyncio.sleep(1)

    try:
        await deps.db.update_parallel_job(job_id, status="running")

        # Update status message
        job = await deps.db.get_parallel_job(job_id)
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=SlackFormatter.parallel_job_status(job),
        )

        # Start cancellation checker
        checker_task = asyncio.create_task(check_cancelled())

        # Run n parallel executions - each gets its own fresh session
        for i in range(n):
            execution_id = f"parallel_{job_id}_{i}"
            task = asyncio.create_task(deps.executor.execute(
                prompt=prompt,
                working_directory=working_directory,
                execution_id=execution_id,
            ))
            execution_tasks.append(task)

        # Wait for all tasks to complete
        results = await asyncio.gather(*execution_tasks, return_exceptions=True)

        # Stop cancellation checker
        cancel_event.set()
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass

        # Check if job was cancelled before processing
        job = await deps.db.get_parallel_job(job_id)
        if job.status == "cancelled":
            return

        # Process results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if isinstance(result, asyncio.CancelledError):
                    processed_results.append({"terminal": i + 1, "error": "Cancelled"})
                else:
                    processed_results.append({"terminal": i + 1, "error": str(result)})
            else:
                processed_results.append({
                    "terminal": i + 1,
                    "output": result.output,
                    "success": result.success,
                    "error": result.error,
                })

        await deps.db.update_parallel_job(job_id, results=processed_results)

        # Create aggregation prompt
        outputs_text = "\n\n".join(
            f"--- Terminal {r['terminal']} ---\n"
            f"{r.get('output', r.get('error', 'No output'))}"
            for r in processed_results
        )

        aggregation_prompt = f"""Aggregate these analyses and create a plan:

{outputs_text}"""

        # Run aggregation in its own fresh session
        agg_result = await deps.executor.execute(
            prompt=aggregation_prompt,
            working_directory=working_directory,
        )

        await deps.db.update_parallel_job(
            job_id,
            status="completed",
            aggregation_output=agg_result.output,
        )

        # Update final status
        job = await deps.db.get_parallel_job(job_id)
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=SlackFormatter.parallel_job_status(job),
        )

        # Send aggregation result as new message
        if agg_result.output:
            output = agg_result.output
            if len(output) > 2900:
                output = output[:2900] + "\n\n... (output truncated)"

            await client.chat_postMessage(
                channel=channel_id,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":brain: Aggregated Analysis",
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": output},
                    },
                ],
            )

    except Exception as e:
        logger.error(f"Parallel execution failed: {e}")
        await deps.db.update_parallel_job(job_id, status="failed")
        await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.error_message(f"Parallel execution failed: {e}"),
        )


async def _run_sequential_execution(
    job_id: int,
    commands: list[str],
    loop_count: int,
    working_directory: str,
    channel_id: str,
    message_ts: str,
    deps: HandlerDependencies,
    client,
    logger,
) -> None:
    """Run sequential command execution with optional looping.

    Each job gets its own independent Claude session to allow concurrent jobs.

    Parameters
    ----------
    job_id : int
        The database job ID.
    commands : list[str]
        List of commands to execute.
    loop_count : int
        Number of times to loop through commands.
    working_directory : str
        Working directory for executions.
    channel_id : str
        Slack channel ID for updates.
    message_ts : str
        Timestamp of the status message to update.
    deps : HandlerDependencies
        Handler dependencies.
    client
        Slack client for API calls.
    logger
        Logger instance.
    """
    try:
        await deps.db.update_parallel_job(job_id, status="running")

        results = []
        # Each job maintains its own session ID for conversation continuity
        job_session_id = None

        for loop_num in range(loop_count):
            for cmd_idx, cmd in enumerate(commands):
                # Check if cancelled
                job = await deps.db.get_parallel_job(job_id)
                if job.status == "cancelled":
                    return

                execution_id = f"sequential_{job_id}_{loop_num}_{cmd_idx}"
                result = await deps.executor.execute(
                    prompt=cmd,
                    working_directory=working_directory,
                    resume_session_id=job_session_id,
                    execution_id=execution_id,
                )

                # Update job-local session ID for subsequent commands
                if result.session_id:
                    job_session_id = result.session_id

                results.append({
                    "loop": loop_num + 1,
                    "command_index": cmd_idx + 1,
                    "command": cmd,
                    "output": result.output,
                    "success": result.success,
                    "error": result.error,
                })

                await deps.db.update_parallel_job(job_id, results=results)

                # Update status in Slack
                job = await deps.db.get_parallel_job(job_id)
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    blocks=SlackFormatter.sequential_job_status(job),
                )

                # Send individual result as threaded reply
                output = result.output or result.error or "No output"
                if len(output) > 2900:
                    output = output[:2900] + "\n\n... (output truncated)"

                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=message_ts,
                    blocks=[
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Loop {loop_num + 1}, Command {cmd_idx + 1}*\n"
                                    f"> {cmd[:100]}{'...' if len(cmd) > 100 else ''}",
                                }
                            ],
                        },
                        {"type": "divider"},
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": output},
                        },
                    ],
                )

        await deps.db.update_parallel_job(job_id, status="completed")

        # Final update
        job = await deps.db.get_parallel_job(job_id)
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=SlackFormatter.sequential_job_status(job),
        )

    except Exception as e:
        logger.error(f"Sequential execution failed: {e}")
        await deps.db.update_parallel_job(job_id, status="failed")
        await client.chat_postMessage(
            channel=channel_id,
            blocks=SlackFormatter.error_message(f"Sequential execution failed: {e}"),
        )
