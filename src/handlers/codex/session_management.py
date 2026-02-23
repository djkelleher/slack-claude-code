"""Codex session management command handlers."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, slack_command


def register_codex_session_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Codex session management commands."""

    @app.command("/codex-clear")
    @slack_command()
    async def handle_codex_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Clear the Codex session (start fresh)."""
        # Clear database session ID
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":broom: Codex session cleared. Next message will start a fresh Codex session.",
                    },
                }
            ],
        )

    @app.command("/codex-sessions")
    @slack_command()
    async def handle_codex_sessions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """List all sessions for this channel."""
        sessions = await deps.db.get_sessions_by_channel(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.session_list(sessions),
        )

    @app.command("/codex-cleanup")
    @slack_command()
    async def handle_codex_cleanup(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Clean up inactive sessions."""
        # Parse days argument
        try:
            days = int(ctx.text) if ctx.text else 30
            if days < 1:
                days = 1
            elif days > 365:
                days = 365
        except ValueError:
            days = 30

        deleted_count = await deps.db.delete_inactive_sessions(days)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.session_cleanup_result(deleted_count, days),
        )

    @app.command("/codex-status")
    @slack_command()
    async def handle_codex_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Show current Codex session status."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
        approval_mode = session.approval_mode or config.CODEX_APPROVAL_MODE
        model = session.model or config.DEFAULT_MODEL or "(default)"
        has_session = ":white_check_mark:" if session.codex_session_id else ":x:"

        fields = [
            {
                "type": "mrkdwn",
                "text": f"*Working Dir:*\n`{session.working_directory}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Model:*\n`{model}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Sandbox:*\n`{sandbox_mode}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Approval:*\n`{approval_mode}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Active Session:*\n{has_session}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Session Type:*\n{'Thread' if session.thread_ts else 'Channel'}",
            },
        ]

        context_text = f"Last active: {session.last_active.strftime('%Y-%m-%d %H:%M:%S')}"

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Codex Session Status*",
                    },
                },
                {
                    "type": "section",
                    "fields": fields,
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": context_text,
                        }
                    ],
                },
            ],
        )
