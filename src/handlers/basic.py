"""Basic command handlers: /cd, /ls, /c."""

import asyncio
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

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

    @app.command("/ls")
    @slack_command()
    async def handle_ls(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /ls [path] command - list directory contents and show cwd."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, config.DEFAULT_WORKING_DIR
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
                blocks=SlackFormatter.error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=SlackFormatter.error_message(f"Not a directory: {target_path}"),
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
                blocks=SlackFormatter.directory_listing(
                    str(target_path), entry_tuples, is_cwd=is_cwd
                ),
            )
        except PermissionError:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Permission denied: {target_path}",
                blocks=SlackFormatter.error_message(f"Permission denied: {target_path}"),
            )

    @app.command("/cd")
    @slack_command()
    async def handle_cd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cd [path] command - change working directory with relative path support."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, config.DEFAULT_WORKING_DIR
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
                blocks=SlackFormatter.error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=SlackFormatter.error_message(f"Not a directory: {target_path}"),
            )
            return

        # Update working directory
        await deps.db.update_session_cwd(ctx.channel_id, str(target_path))
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Working directory updated to: {target_path}",
            blocks=SlackFormatter.cwd_updated(str(target_path)),
        )

    @app.command("/c")
    @slack_command(require_text=True, usage_hint="Usage: /c <prompt>")
    async def handle_claude(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /c <prompt> command - send prompt to Claude Code."""
        prompt = ctx.text

        # Get or create session
        session = await deps.db.get_or_create_session(
            ctx.channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create command history entry
        cmd_history = await deps.db.add_command(session.id, prompt)
        await deps.db.update_command_status(cmd_history.id, "running")

        # Send initial processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Processing: {prompt[:100]}...",
            blocks=SlackFormatter.processing_message(prompt),
        )
        message_ts = response["ts"]

        # Execute command with streaming updates
        accumulated_output = ""
        last_update_time = 0
        execution_id = str(uuid.uuid4())

        async def on_chunk(msg):
            nonlocal accumulated_output, last_update_time

            if msg.type == "assistant" and msg.content:
                # Limit accumulated output to prevent memory issues
                if len(accumulated_output) < config.timeouts.streaming.max_accumulated_size:
                    accumulated_output += msg.content

                # Rate limit updates to avoid Slack API limits
                current_time = asyncio.get_running_loop().time()
                if current_time - last_update_time > config.timeouts.slack.message_update_throttle:
                    last_update_time = current_time
                    try:
                        await ctx.client.chat_update(
                            channel=ctx.channel_id,
                            ts=message_ts,
                            text=accumulated_output[:100] + "..." if len(accumulated_output) > 100 else accumulated_output,
                            blocks=SlackFormatter.streaming_update(
                                prompt, accumulated_output
                            ),
                        )
                    except Exception as e:
                        ctx.logger.warning(f"Failed to update message: {e}")

        try:
            result = await deps.executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=ctx.channel_id,
                resume_session_id=session.claude_session_id,  # Resume previous session if exists
                execution_id=execution_id,
                on_chunk=on_chunk,
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(ctx.channel_id, result.session_id)

            # Update command history
            if result.success:
                await deps.db.update_command_status(
                    cmd_history.id, "completed", result.output
                )
            else:
                await deps.db.update_command_status(
                    cmd_history.id, "failed", result.output, result.error
                )

            # Send final response
            output = result.output or result.error or "No output"

            if SlackFormatter.should_attach_file(output):
                # Large response - attach as file
                blocks, file_content, file_title = SlackFormatter.command_response_with_file(
                    prompt=prompt,
                    output=output,
                    command_id=cmd_history.id,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                )
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=message_ts,
                    text=output[:100] + "..." if len(output) > 100 else output,
                    blocks=blocks,
                )
                # Upload file
                try:
                    await ctx.client.files_upload_v2(
                        channel=ctx.channel_id,
                        content=file_content,
                        filename=file_title,
                        title=file_title,
                        thread_ts=message_ts,
                    )
                except Exception as upload_error:
                    ctx.logger.error(f"Failed to upload file: {upload_error}")
                    error_msg = str(upload_error)
                    if "missing_scope" in error_msg and "files:write" in error_msg:
                        await ctx.client.chat_postMessage(
                            channel=ctx.channel_id,
                            thread_ts=message_ts,
                            text="⚠️ Could not upload file: Missing `files:write` scope. Please add this scope in your Slack app configuration (OAuth & Permissions).",
                        )
                    else:
                        await ctx.client.chat_postMessage(
                            channel=ctx.channel_id,
                            thread_ts=message_ts,
                            text=f"⚠️ Could not upload file: {error_msg}",
                        )
            else:
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=message_ts,
                    text=output[:100] + "..." if len(output) > 100 else output,
                    blocks=SlackFormatter.command_response(
                        prompt=prompt,
                        output=output,
                        command_id=cmd_history.id,
                        duration_ms=result.duration_ms,
                        cost_usd=result.cost_usd,
                        is_error=not result.success,
                    ),
                )

        except Exception as e:
            ctx.logger.error(f"Error executing command: {e}")
            await deps.db.update_command_status(
                cmd_history.id, "failed", error_message=str(e)
            )
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=SlackFormatter.error_message(str(e)),
            )
