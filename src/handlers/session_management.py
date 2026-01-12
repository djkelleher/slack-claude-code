"""Session management command handlers: /sessions, /session-cleanup."""

from slack_bolt.async_app import AsyncApp

from src.utils.formatters.session import session_cleanup_result, session_list

from .base import CommandContext, HandlerDependencies, slack_command


def register_session_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register session management command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/sessions")
    @slack_command()
    async def handle_sessions_list(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /sessions command - list all sessions for this channel.

        Lists both channel-level session and all thread sessions.
        """
        sessions = await deps.db.get_sessions_by_channel(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Found {len(sessions)} session(s)",
            blocks=session_list(sessions),
        )

    @app.command("/session-cleanup")
    @slack_command()
    async def handle_session_cleanup(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /session-cleanup command - delete inactive sessions.

        Deletes sessions that have been inactive for more than 30 days.
        """
        deleted_count = await deps.db.delete_inactive_sessions(inactive_days=30)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Cleaned up {deleted_count} session(s)",
            blocks=session_cleanup_result(deleted_count, inactive_days=30),
        )
