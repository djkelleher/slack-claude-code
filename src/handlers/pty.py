"""PTY session command handlers: /pty."""

from slack_bolt.async_app import AsyncApp

from src.pty.pool import PTYSessionPool

from .base import CommandContext, HandlerDependencies, slack_command


def register_pty_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register PTY session command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies (unused but kept for consistency).
    """

    @app.command("/pty")
    @slack_command()
    async def handle_pty(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /pty command - show PTY session status."""
        session_info = PTYSessionPool.get_session_info(ctx.channel_id)

        if not session_info:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=":grey_question: No active PTY session for this channel. "
                "One will be created on your next command.",
            )
            return

        state_emoji = {
            "idle": ":green_circle:",
            "busy": ":large_blue_circle:",
            "awaiting_approval": ":yellow_circle:",
            "error": ":red_circle:",
        }.get(session_info.get("state", ""), ":grey_question:")

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":computer: PTY Session Status",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Session ID:* `{session_info.get('session_id', 'N/A')}`",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*State:* {state_emoji} {session_info.get('state', 'unknown')}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*PID:* {session_info.get('pid', 'N/A')}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Working Dir:* `{session_info.get('working_directory', 'N/A')}`",
                        },
                    ],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Restart Session"},
                            "style": "danger",
                            "action_id": "restart_pty",
                            "value": ctx.channel_id,
                        }
                    ],
                },
            ],
        )
