"""Job status and cancellation command handlers: /st, /cc."""

from slack_bolt.async_app import AsyncApp

from src.utils.execution_scope import build_session_scope
from src.utils.formatters.command import error_message
from src.utils.formatters.job import job_status_list, job_status_summary_text

from ..base import CommandContext, HandlerDependencies, slack_command


def register_parallel_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register job status and cancellation command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/st")
    @slack_command()
    async def handle_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /st command - show active jobs."""
        jobs = await deps.db.get_active_jobs(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=job_status_summary_text(jobs),
            blocks=job_status_list(jobs),
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
                    blocks=error_message("Invalid job ID. Usage: /cc [job_id]"),
                )
        else:
            # Cancel all active jobs in channel
            jobs = await deps.db.get_active_jobs(ctx.channel_id)
            cancelled_count = 0
            for job in jobs:
                if await deps.db.cancel_job(job.id):
                    cancelled_count += 1

            # Also cancel active executions in this scope only
            if ctx.thread_ts:
                session_scope = build_session_scope(ctx.channel_id, ctx.thread_ts)
                executor_cancelled = await deps.executor.cancel_by_scope(session_scope)
            else:
                executor_cancelled = await deps.executor.cancel_by_channel(ctx.channel_id)

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":no_entry: Cancelled {cancelled_count} job(s) and "
                f"{executor_cancelled} active execution(s).",
            )
