"""Basic command handlers: /cwd."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter
from src.utils.validators import validate_path

from .base import CommandContext, HandlerDependencies, slack_command


def register_basic_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register basic command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/cwd")
    @slack_command()
    async def handle_cwd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cwd [path] command - show or set working directory."""
        if not ctx.text:
            # Show current working directory
            session = await deps.db.get_or_create_session(
                ctx.channel_id, config.DEFAULT_WORKING_DIR
            )
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":file_folder: Current working directory: `{session.working_directory}`",
            )
            return

        valid, result = validate_path(ctx.text)
        if not valid:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(result),
            )
            return

        await deps.db.update_session_cwd(ctx.channel_id, str(result))
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.cwd_updated(str(result)),
        )
