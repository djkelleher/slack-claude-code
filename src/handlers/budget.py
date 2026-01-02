"""Budget and usage command handlers: /usage, /budget."""

from slack_bolt.async_app import AsyncApp

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
        """Handle /usage command - show current Pro plan usage.

        This handler manages its own error handling due to async message updates.
        """
        await ack()

        channel_id = command["channel_id"]

        # Send initial message
        response = await client.chat_postMessage(
            channel=channel_id,
            text=":hourglass: Checking usage...",
        )

        try:
            snapshot = await deps.usage_checker.get_usage()

            if snapshot:
                # Get current threshold
                threshold = deps.budget_scheduler.get_current_threshold()
                is_night = deps.budget_scheduler.is_nighttime()
                period = "night" if is_night else "day"

                # Usage bar visualization
                bar_length = 20
                filled = int(snapshot.usage_percent / 100 * bar_length)
                bar = "█" * filled + "░" * (bar_length - filled)

                status = (
                    ":white_check_mark:"
                    if snapshot.usage_percent < threshold
                    else ":warning:"
                )

                await client.chat_update(
                    channel=channel_id,
                    ts=response["ts"],
                    blocks=[
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": ":chart_with_upwards_trend: Claude Pro Usage",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Usage:* {snapshot.usage_percent:.1f}% {status}\n`[{bar}]`",
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Reset:* {snapshot.reset_time or 'Unknown'}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Threshold ({period}):* {threshold:.0f}%",
                                },
                            ],
                        },
                    ],
                )
            else:
                await client.chat_update(
                    channel=channel_id,
                    ts=response["ts"],
                    text=":warning: Could not retrieve usage information. "
                    "Make sure `claude usage` command is available.",
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
            text=f":white_check_mark: Updated {period} threshold to {percent:.0f}%",
        )
