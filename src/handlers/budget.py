"""Budget and usage command handlers: /usage, /budget."""

import uuid

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

from .base import CommandContext, HandlerDependencies, slack_command


def register_budget_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register budget and usage command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/usage")
    async def handle_usage(ack, command, client, logger):
        """Handle /usage command - show plan usage limits and rate limit status.

        Passes through to Claude CLI /usage command for subscription plan info.
        """
        await ack()

        channel_id = command["channel_id"]
        thread_ts = command.get("thread_ts")

        # Send initial message
        response = await client.chat_postMessage(
            channel=channel_id,
            text=":hourglass: Checking usage...",
        )

        try:
            # Get session for Claude CLI passthrough
            session = await deps.db.get_or_create_session(
                channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
            )

            # Execute /usage via Claude CLI
            result = await deps.executor.execute(
                prompt="/usage",
                working_directory=session.working_directory,
                session_id=channel_id,
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                model=session.model,
            )

            # Update session if needed
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

            output = result.output or result.error or "No usage information available."

            await client.chat_update(
                channel=channel_id,
                ts=response["ts"],
                text=output[:100] + "..." if len(output) > 100 else output,
                blocks=SlackFormatter.command_response(
                    prompt="/usage",
                    output=output,
                    command_id=None,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                ),
            )

        except Exception as e:
            logger.error(f"Error checking usage: {e}")
            await client.chat_update(
                channel=channel_id,
                ts=response["ts"],
                blocks=SlackFormatter.error_message(f"Failed to check usage: {e}"),
            )

    @app.command("/budget")
    @slack_command()
    async def handle_budget(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /budget [day|night] <percent> command."""
        if not ctx.text:
            # Show current thresholds
            thresholds = deps.budget_scheduler.thresholds
            is_night = deps.budget_scheduler.is_nighttime()
            current = "night" if is_night else "day"

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":moneybag: Budget Thresholds",
                            "emoji": True,
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Day threshold:* {thresholds.day_threshold:.0f}%"
                                f"{' (active)' if current == 'day' else ''}",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Night threshold:* {thresholds.night_threshold:.0f}%"
                                f"{' (active)' if current == 'night' else ''}",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Night hours:* {thresholds.night_start_hour}:00 - "
                                f"{thresholds.night_end_hour}:00",
                            },
                        ],
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Update with: `/budget day 85` or `/budget night 95`",
                            }
                        ],
                    },
                ],
            )
            return

        # Parse arguments
        parts = ctx.text.split()
        if len(parts) != 2:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(
                    "Usage: `/budget day <percent>` or `/budget night <percent>`"
                ),
            )
            return

        period, percent_str = parts

        if period not in ("day", "night"):
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message("Period must be 'day' or 'night'."),
            )
            return

        try:
            percent = float(percent_str)
            if not 0 <= percent <= 100:
                raise ValueError("Percent must be between 0 and 100")
        except ValueError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(f"Invalid percent: {e}"),
            )
            return

        # Update threshold
        if period == "day":
            deps.budget_scheduler.thresholds.day_threshold = percent
        else:
            deps.budget_scheduler.thresholds.night_threshold = percent

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f":heavy_check_mark: Updated {period} threshold to {percent:.0f}%",
        )
