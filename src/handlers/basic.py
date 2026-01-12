"""Basic command handlers: /cd, /ls, /c."""

import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.detail_cache import DetailCache
from src.utils.formatting import SlackFormatter
from src.utils.slack_helpers import post_text_snippet
from src.utils.streaming import StreamingMessageState, create_streaming_callback

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
        await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, str(target_path))
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Working directory updated to: {target_path}",
            blocks=SlackFormatter.cwd_updated(str(target_path)),
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

    @app.command("/c")
    @slack_command(require_text=True, usage_hint="Usage: /c <prompt>")
    async def handle_claude(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /c <prompt> command - send prompt to Claude Code."""
        prompt = ctx.text

        # Get or create session
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
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

        # Setup streaming state
        execution_id = str(uuid.uuid4())
        streaming_state = StreamingMessageState(
            channel_id=ctx.channel_id,
            message_ts=message_ts,
            prompt=prompt,
            client=ctx.client,
            logger=ctx.logger,
            smart_concat=True,
            track_tools=True,  # Track tools to detect ExitPlanMode
        )
        streaming_state.start_heartbeat()
        on_chunk = create_streaming_callback(streaming_state)

        try:
            result = await deps.executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=ctx.channel_id,
                resume_session_id=session.claude_session_id,  # Resume previous session if exists
                execution_id=execution_id,
                on_chunk=on_chunk,
                permission_mode=session.permission_mode,  # Per-session mode
                db_session_id=session.id,  # Smart context tracking
                model=session.model,  # Per-session model
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(
                    ctx.channel_id, ctx.thread_ts, result.session_id
                )

            # Update command history
            if result.success:
                await deps.db.update_command_status(cmd_history.id, "completed", result.output)
            else:
                await deps.db.update_command_status(
                    cmd_history.id, "failed", result.output, result.error
                )

            # Send final response
            output = result.output or result.error or "No output"

            # Check if we were in plan mode (before any auto-exit)
            was_in_plan_mode = session.permission_mode == "plan"

            # In plan mode, use detailed output (includes tool use details) if available
            display_output = output
            plan_file_path = None
            if was_in_plan_mode and result.detailed_output:
                display_output = result.detailed_output

                # Try to find the plan file that was created
                import os
                from pathlib import Path

                plan_dir = Path.home() / ".claude" / "plans"
                if plan_dir.exists():
                    # Find the most recently modified .md file
                    plan_files = list(plan_dir.glob("*.md"))
                    if plan_files:
                        plan_file_path = max(plan_files, key=lambda p: p.stat().st_mtime)
                        ctx.logger.info(f"Found plan file: {plan_file_path}")

            if SlackFormatter.should_attach_file(display_output):
                # Large response - attach as file
                blocks, file_content, file_title = SlackFormatter.command_response_with_file(
                    prompt=prompt,
                    output=display_output,
                    command_id=cmd_history.id,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                )
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=message_ts,
                    text=(
                        display_output[:100] + "..."
                        if len(display_output) > 100
                        else display_output
                    ),
                    blocks=blocks,
                )
                # Post response content
                try:
                    # Post summary as inline snippet
                    await post_text_snippet(
                        client=ctx.client,
                        channel_id=ctx.channel_id,
                        content=file_content,
                        title="📄 Response summary",
                    )
                    # Store detailed output and post button to view it
                    if result.detailed_output and result.detailed_output != output:
                        DetailCache.store(cmd_history.id, result.detailed_output)
                        await ctx.client.chat_postMessage(
                            channel=ctx.channel_id,
                            text="📋 Detailed output available",
                            blocks=[
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": f"📋 *Detailed output* ({len(result.detailed_output):,} chars)",
                                    },
                                    "accessory": {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "View Details",
                                            "emoji": True,
                                        },
                                        "action_id": "view_detailed_output",
                                        "value": str(cmd_history.id),
                                    },
                                },
                            ],
                        )
                except Exception as post_error:
                    ctx.logger.error(f"Failed to post snippet: {post_error}")
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"⚠️ Could not post detailed output: {str(post_error)[:100]}",
                    )
            else:
                await ctx.client.chat_update(
                    channel=ctx.channel_id,
                    ts=message_ts,
                    text=(
                        display_output[:100] + "..."
                        if len(display_output) > 100
                        else display_output
                    ),
                    blocks=SlackFormatter.command_response(
                        prompt=prompt,
                        output=display_output,
                        command_id=cmd_history.id,
                        duration_ms=result.duration_ms,
                        cost_usd=result.cost_usd,
                        is_error=not result.success,
                    ),
                )

            # Attach plan file if in plan mode and file was found
            if plan_file_path and plan_file_path.exists():
                try:
                    with open(plan_file_path, "r") as f:
                        plan_content = f.read()

                    await ctx.client.files_upload_v2(
                        channel=ctx.channel_id,
                        file=str(plan_file_path),
                        title=f"📋 Implementation Plan: {plan_file_path.stem}",
                        initial_comment="*Implementation Plan*",
                        filename=plan_file_path.name,
                    )
                    ctx.logger.info(f"Uploaded plan file to Slack: {plan_file_path}")
                except Exception as upload_error:
                    ctx.logger.error(f"Failed to upload plan file: {upload_error}")
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"⚠️ Could not attach plan file: {str(upload_error)[:100]}",
                    )

            # Check if plan mode should auto-exit
            # If we were in plan mode and ExitPlanMode was called (even if failed),
            # automatically switch to bypass mode
            if was_in_plan_mode:
                # Check if ExitPlanMode tool was attempted
                exit_plan_attempted = any(
                    tool.name == "ExitPlanMode" for tool in streaming_state.get_tool_list()
                )

                if exit_plan_attempted:
                    ctx.logger.info("ExitPlanMode detected, switching session to bypass mode")
                    await deps.db.update_session_mode(
                        ctx.channel_id, ctx.thread_ts, "bypassPermissions"
                    )

                    # Post notification
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text="Plan completed - switching to execution mode",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": ":white_check_mark: *Plan completed!* Automatically switched to bypass mode for execution.\n\n_Use `/mode plan` to return to planning mode if needed._",
                                },
                            }
                        ],
                    )

        except Exception as e:
            ctx.logger.error(f"Error executing command: {e}")
            await deps.db.update_command_status(cmd_history.id, "failed", error_message=str(e))
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=SlackFormatter.error_message(str(e)),
            )
