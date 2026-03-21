"""Basic command handlers: /!, /cd, /ls, /pwd."""

import asyncio
from pathlib import Path
from time import monotonic

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.handlers.response_delivery import deliver_command_response
from src.utils.formatters.command import error_message
from src.utils.formatters.directory import cwd_updated, directory_listing
from src.utils.formatters.streaming import processing_message

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

    @app.command("/!")
    @slack_command(require_text=True, usage_hint="Usage: /! <bash command>")
    async def handle_bang(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle `/!` by executing a bash command directly on the host."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        cmd_history = await deps.db.add_command(session.id, f"/! {ctx.text}")
        await deps.db.update_command_status(cmd_history.id, "running")

        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Running: {ctx.text}",
            blocks=processing_message(ctx.text),
        )
        message_ts = response["ts"]

        started_at = monotonic()
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            ctx.text,
            cwd=session.working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        duration_ms = int((monotonic() - started_at) * 1000)

        stdout_text = stdout.decode(errors="replace").strip()
        stderr_text = stderr.decode(errors="replace").strip()
        output = stdout_text
        if stderr_text:
            output = f"{output}\n\n[stderr]\n{stderr_text}".strip()
        if not output:
            output = "Command completed with no output."

        if process.returncode == 0:
            await deps.db.update_command_status(cmd_history.id, "completed", output=output)
        else:
            await deps.db.update_command_status(
                cmd_history.id,
                "failed",
                output=output,
                error_message=f"Command exited with status {process.returncode}",
            )

        await deliver_command_response(
            client=ctx.client,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            message_ts=message_ts,
            prompt=f"/! {ctx.text}",
            output=output,
            command_id=cmd_history.id,
            duration_ms=duration_ms,
            cost_usd=None,
            is_error=process.returncode != 0,
            logger=ctx.logger,
            terminal_style=True,
        )

    @app.command("/ls")
    @slack_command()
    async def handle_ls(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /ls [path] command - list directory contents and show cwd."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        base_path = Path(session.working_directory).expanduser()

        # Resolve target path (relative or absolute)
        is_cwd = not ctx.text
        if ctx.text:
            target_path = (base_path / ctx.text).resolve()
        else:
            target_path = base_path.resolve()

        # Validate path exists and is a directory
        if not target_path.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Path does not exist: {target_path}",
                blocks=error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=error_message(f"Not a directory: {target_path}"),
            )
            return

        # List directory contents
        try:
            entries = list(target_path.iterdir())
            # Sort: directories first, then files, alphabetically
            dirs = sorted([e for e in entries if e.is_dir()], key=lambda x: x.name.lower())
            files = sorted([e for e in entries if e.is_file()], key=lambda x: x.name.lower())
            sorted_entries = dirs + files

            # Convert to (name, is_dir) tuples for formatter
            entry_tuples = [(e.name, e.is_dir()) for e in sorted_entries]

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Contents of {target_path}",
                blocks=directory_listing(str(target_path), entry_tuples, is_cwd=is_cwd),
            )
        except OSError as e:
            # OSError is parent of PermissionError, FileNotFoundError, etc.
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: {e}",
                blocks=error_message(f"Cannot access directory: {e}"),
            )

    @app.command("/cd")
    @slack_command()
    async def handle_cd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cd [path] command - change working directory with relative path support."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        if not ctx.text:
            # No argument - show current directory
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":file_folder: Current working directory: `{session.working_directory}`",
            )
            return

        # Resolve path relative to current working directory
        base_path = Path(session.working_directory).expanduser()
        target_path = (base_path / ctx.text).resolve()

        # Validate path exists and is a directory
        if not target_path.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Path does not exist: {target_path}",
                blocks=error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=error_message(f"Not a directory: {target_path}"),
            )
            return

        # Update working directory
        await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, str(target_path))
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Working directory updated to: {target_path}",
            blocks=cwd_updated(str(target_path)),
        )

    @app.command("/pwd")
    @slack_command()
    async def handle_pwd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /pwd command - print current working directory."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f":file_folder: Current working directory: `{session.working_directory}`",
        )
