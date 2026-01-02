"""Claude CLI passthrough command handlers."""

import uuid

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

from .base import CommandContext, HandlerDependencies, slack_command


def register_claude_cli_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Claude CLI passthrough command handlers.

    These commands pass through to the Claude Code CLI commands.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    async def _send_claude_command(
        ctx: CommandContext,
        claude_command: str,
        deps: HandlerDependencies,
    ) -> None:
        """Send a Claude CLI command and return the result.

        Parameters
        ----------
        ctx : CommandContext
            The command context.
        claude_command : str
            The Claude CLI command to execute (e.g., "/clear", "/cost").
        deps : HandlerDependencies
            Handler dependencies.
        """
        session = await deps.db.get_or_create_session(
            ctx.channel_id, config.DEFAULT_WORKING_DIR
        )

        # Send processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Running: {claude_command}",
            blocks=SlackFormatter.processing_message(claude_command),
        )
        message_ts = response["ts"]

        try:
            result = await deps.executor.execute(
                prompt=claude_command,
                working_directory=session.working_directory,
                session_id=ctx.channel_id,
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
            )

            # Update session if needed
            if result.session_id:
                await deps.db.update_session_claude_id(ctx.channel_id, result.session_id)

            output = result.output or result.error or "Command completed (no output)"

            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=output[:100] + "..." if len(output) > 100 else output,
                blocks=SlackFormatter.command_response(
                    prompt=claude_command,
                    output=output,
                    command_id=None,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                ),
            )

        except Exception as e:
            ctx.logger.error(f"Claude CLI command failed: {e}")
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=SlackFormatter.error_message(str(e)),
            )

    @app.command("/clear")
    @slack_command()
    async def handle_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /clear command - reset Claude conversation."""
        await _send_claude_command(ctx, "/clear", deps)

    @app.command("/add-dir")
    @slack_command(require_text=True, usage_hint="Usage: /add-dir <path>")
    async def handle_add_dir(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /add-dir <path> command - add directory to context."""
        await _send_claude_command(ctx, f"/add-dir {ctx.text}", deps)

    @app.command("/compact")
    @slack_command()
    async def handle_compact(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /compact [instructions] command - compact conversation."""
        if ctx.text:
            await _send_claude_command(ctx, f"/compact {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/compact", deps)

    @app.command("/cost")
    @slack_command()
    async def handle_cost(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cost command - show session cost."""
        await _send_claude_command(ctx, "/cost", deps)

    @app.command("/claude-help")
    @slack_command()
    async def handle_claude_help(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /claude-help command - show Claude Code help."""
        await _send_claude_command(ctx, "/help", deps)

    @app.command("/doctor")
    @slack_command()
    async def handle_doctor(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /doctor command - run Claude Code diagnostics."""
        await _send_claude_command(ctx, "/doctor", deps)

    @app.command("/claude-config")
    @slack_command()
    async def handle_claude_config(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /claude-config command - show Claude Code config."""
        await _send_claude_command(ctx, "/config", deps)
