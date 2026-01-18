"""Claude CLI passthrough command handlers."""

import asyncio
import signal
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
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
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
                permission_mode=session.permission_mode,
                model=session.model,
            )

            # Update session if needed
            if result.session_id:
                await deps.db.update_session_claude_id(
                    ctx.channel_id, ctx.thread_ts, result.session_id
                )

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
        """Handle /clear command - cancel processes and reset Claude conversation."""
        # Step 1: Cancel all active executor processes for this channel
        cancelled_count = 0
        active_processes = deps.executor._active_processes
        # Cancel processes where execution_id contains channel_id
        for exec_id, process in list(active_processes.items()):
            if ctx.channel_id in exec_id:
                process.terminate()
                cancelled_count += 1
                ctx.logger.info(f"Terminated process: {exec_id}")

        # Brief wait for graceful shutdown
        if cancelled_count > 0:
            await asyncio.sleep(0.5)

        # Step 2: Clear the Claude session ID so next message starts fresh
        await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
        ctx.logger.info("Cleared Claude session ID")

        # Step 3: Send /clear command to Claude CLI (existing flow)
        await _send_claude_command(ctx, "/clear", deps)

        # Step 4: Notify user if processes were cancelled
        if cancelled_count > 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Cancelled {cancelled_count} active process(es) and cleared Claude session.",
            )

    @app.command("/esc")
    @slack_command()
    async def handle_esc(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /esc command - interrupt current operation (like pressing Escape)."""
        logger = logging.getLogger(__name__)

        # Cancel all active executor processes for this channel
        cancelled_count = 0
        active_processes = deps.executor._active_processes
        # Cancel processes where execution_id contains channel_id
        for exec_id, process in list(active_processes.items()):
            if ctx.channel_id in exec_id:
                # Send SIGINT first (like Ctrl+C / Escape)
                try:
                    process.send_signal(signal.SIGINT)
                except (ProcessLookupError, OSError):
                    logger.debug(f"Process {exec_id} already terminated")
                cancelled_count += 1
                ctx.logger.info(f"Sent interrupt to process: {exec_id}")

        if cancelled_count > 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":stop_sign: Interrupted {cancelled_count} running operation(s).",
            )
        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=":information_source: No active operations to interrupt.",
            )

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

    @app.command("/context")
    @slack_command()
    async def handle_context(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /context command - visualize current context usage."""
        await _send_claude_command(ctx, "/context", deps)

    @app.command("/model")
    @slack_command()
    async def handle_model(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /model [name] command - show or change AI model."""
        # Get session to check/update model
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        if ctx.text:
            # Direct model selection via command argument
            model_name = ctx.text.strip().lower()
            # Normalize model names
            model_map = {
                "opus": "opus",
                "opus-4": "opus",
                "opus-4.5": "opus",
                "sonnet": "sonnet",
                "sonnet-4": "sonnet",
                "sonnet-4.5": "sonnet",
                "haiku": "haiku",
                "haiku-4": "haiku",
            }
            normalized = model_map.get(model_name, model_name)
            await deps.db.update_session_model(ctx.channel_id, ctx.thread_ts, normalized)
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":heavy_check_mark: Model changed to *{normalized}*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":heavy_check_mark: Model changed to *{normalized}*",
                        },
                    }
                ],
            )
        else:
            # Show current model and allow selection via buttons
            # Get current model from session (default to opus)
            current_model = session.model or "opus"

            # Available models (opus first as default)
            models = [
                {"name": "opus", "display": "Claude Opus 4.5", "desc": "Most capable model"},
                {
                    "name": "sonnet",
                    "display": "Claude Sonnet 4.5",
                    "desc": "Balanced performance and speed",
                },
                {
                    "name": "haiku",
                    "display": "Claude Haiku 4",
                    "desc": "Fastest and most cost-effective",
                },
            ]

            # Get display name for current model
            current_display = next(
                (m["display"] for m in models if m["name"] == current_model),
                current_model,
            )

            # Build button blocks
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Current Model:* {current_display}\n\nSelect a model:",
                    },
                },
                {"type": "divider"},
            ]

            for model in models:
                is_current = model["name"] == current_model
                button_text = f"{'✓ ' if is_current else ''}{model['display']}"

                # Build button accessory
                button_accessory = {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": button_text,
                        "emoji": True,
                    },
                    "action_id": f"select_model_{model['name']}",
                    "value": f"{ctx.channel_id}|{ctx.thread_ts or ''}",
                }

                # Only add style if it's the current model
                if is_current:
                    button_accessory["style"] = "primary"

                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{model['display']}*\n{model['desc']}",
                        },
                        "accessory": button_accessory,
                    }
                )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current model: {current_model}",
                blocks=blocks,
            )

    @app.command("/resume")
    @slack_command()
    async def handle_resume(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /resume [session] command - resume a previous session."""
        if ctx.text:
            await _send_claude_command(ctx, f"/resume {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/resume", deps)

    @app.command("/init")
    @slack_command()
    async def handle_init(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /init command - initialize project with CLAUDE.md."""
        await _send_claude_command(ctx, "/init", deps)

    @app.command("/memory")
    @slack_command()
    async def handle_memory(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /memory command - edit CLAUDE.md memory files."""
        await _send_claude_command(ctx, "/memory", deps)

    @app.command("/review")
    @slack_command()
    async def handle_review(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /review command - request code review."""
        await _send_claude_command(ctx, "/review", deps)

    @app.command("/permissions")
    @slack_command()
    async def handle_permissions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /permissions command - view or update permissions."""
        await _send_claude_command(ctx, "/permissions", deps)

    @app.command("/stats")
    @slack_command()
    async def handle_stats(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /stats command - show usage stats and history."""
        await _send_claude_command(ctx, "/stats", deps)

    @app.command("/todos")
    @slack_command()
    async def handle_todos(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /todos command - list current TODO items."""
        await _send_claude_command(ctx, "/todos", deps)

    @app.command("/mcp")
    @slack_command()
    async def handle_mcp(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mcp command - show MCP server configuration."""
        if ctx.text:
            await _send_claude_command(ctx, f"/mcp {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/mcp", deps)
