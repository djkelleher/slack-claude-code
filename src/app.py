#!/usr/bin/env python3
"""
Slack Claude Code Bot - Main Application Entry Point

A Slack app that allows running Claude Code CLI commands from Slack,
with each channel representing a separate persistent PTY session.
"""

import asyncio
import logging
import os
import signal
import sys
import traceback
import uuid

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from src.config import config
from src.database.migrations import init_database
from src.database.repository import DatabaseRepository
from src.claude.subprocess_executor import SubprocessExecutor
from src.handlers import register_commands
from src.handlers.actions import register_actions
from src.utils.file_downloader import (
    FileTooLargeError,
    FileDownloadError,
    download_slack_file,
)
from src.utils.formatting import SlackFormatter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def shutdown(executor: SubprocessExecutor) -> None:
    """Graceful shutdown: cleanup active processes."""
    logger.info("Shutting down - cleaning up active processes...")
    await executor.shutdown()
    logger.info("All processes terminated")


async def main():
    """Main application entry point."""
    # Validate configuration
    errors = config.validate()
    if errors:
        logger.error("Configuration errors:")
        for error in errors:
            logger.error(f"  - {error}")
        sys.exit(1)

    # Initialize database
    logger.info(f"Initializing database at {config.DATABASE_PATH}")
    await init_database(config.DATABASE_PATH)

    # Create app components
    db = DatabaseRepository(config.DATABASE_PATH)
    executor = SubprocessExecutor(db=db)  # Pass db for smart context tracking

    # Create Slack app
    app = AsyncApp(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    # Register handlers
    deps = register_commands(app, db, executor)
    register_actions(app, deps)

    # Add a simple health check
    @app.event("app_mention")
    async def handle_mention(event, say, logger):
        """Respond to @mentions."""
        await say(
            text="Hi! I'm Claude Code Bot. Just send me a message to run commands."
        )

    @app.event("message")
    async def handle_message(event, client, logger):
        """Handle messages and pipe them to Claude Code."""
        logger.info(f"Message event received: {event.get('text', '')[:50]}...")

        # Ignore bot messages to avoid responding to ourselves
        if event.get("bot_id") or event.get("subtype"):
            logger.debug(f"Ignoring bot/subtype message: bot_id={event.get('bot_id')}, subtype={event.get('subtype')}")
            return

        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts")  # Extract thread timestamp
        prompt = event.get("text", "").strip()
        files = event.get("files", [])  # Extract uploaded files

        # Allow messages with files but no text
        if not prompt and not files:
            logger.debug("Empty prompt and no files, ignoring")
            return

        # Get or create session (thread-aware)
        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        logger.info(f"Using session: {session.session_display_name()}")

        # Process file uploads
        uploaded_files = []
        if files:
            logger.info(f"Processing {len(files)} uploaded file(s)")

            # Create .slack_uploads directory in session working directory
            uploads_dir = os.path.join(session.working_directory, ".slack_uploads")
            os.makedirs(uploads_dir, exist_ok=True)

            for file_info in files:
                try:
                    logger.info(f"Downloading file: {file_info.get('name')}")

                    # Download file
                    local_path, metadata = await download_slack_file(
                        client=client,
                        file_id=file_info["id"],
                        slack_bot_token=config.SLACK_BOT_TOKEN,
                        destination_dir=uploads_dir,
                        max_size_bytes=config.MAX_FILE_SIZE_MB * 1024 * 1024,
                    )

                    # Track in database
                    uploaded_file = await deps.db.add_uploaded_file(
                        session_id=session.id,
                        slack_file_id=file_info["id"],
                        filename=file_info["name"],
                        local_path=local_path,
                        mimetype=file_info.get("mimetype", ""),
                        size=file_info.get("size", 0),
                    )
                    uploaded_files.append(uploaded_file)
                    logger.info(f"File downloaded and tracked: {local_path}")

                    # Track in file context for smart context
                    await deps.db.track_file_context(
                        session.id, local_path, "uploaded"
                    )

                    # For images, show thumbnail in thread
                    if file_info.get("mimetype", "").startswith("image/"):
                        thumb_url = file_info.get("thumb_360") or file_info.get("thumb_160")
                        if thumb_url:
                            await client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=thread_ts or event.get("ts"),  # Use message ts if not in thread
                                text=f"📎 Uploaded: {file_info['name']}",
                                blocks=[
                                    {
                                        "type": "section",
                                        "text": {
                                            "type": "mrkdwn",
                                            "text": f":frame_with_picture: Uploaded image: *{file_info['name']}*",
                                        },
                                    },
                                    {
                                        "type": "image",
                                        "image_url": thumb_url,
                                        "alt_text": file_info["name"],
                                    },
                                ],
                            )

                except FileTooLargeError as e:
                    logger.warning(f"File too large: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ File too large: {file_info['name']} ({e.size_mb:.1f}MB, max: {e.max_mb}MB)",
                    )
                except FileDownloadError as e:
                    logger.error(f"File download failed: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Failed to download file: {file_info['name']} - {str(e)}",
                    )
                except Exception as e:
                    logger.error(f"Unexpected error processing file {file_info.get('name')}: {e}\n{traceback.format_exc()}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Error processing file: {file_info['name']} - {str(e)}",
                    )

        # Enhance prompt with uploaded file references
        if uploaded_files:
            file_refs = "\n".join([
                f"- {f.filename} (at {f.local_path})"
                for f in uploaded_files
            ])

            if prompt:
                prompt = f"{prompt}\n\nUploaded files:\n{file_refs}"
            else:
                # No text, only files - provide default prompt
                prompt = f"Please analyze these uploaded files:\n{file_refs}"

        # Smart context: Add recently used files to prompt
        try:
            # Get files you've worked with recently (use_count >= 2 or auto_include)
            file_contexts = await deps.db.get_file_context(session.id)

            if file_contexts:
                # Limit to top 5 most relevant files
                relevant_files = sorted(
                    file_contexts,
                    key=lambda f: (f.use_count, f.last_used),
                    reverse=True
                )[:5]

                # Build context summary
                from datetime import datetime, timezone
                context_lines = []
                for fc in relevant_files:
                    # Calculate time ago
                    # Ensure both datetimes are timezone-aware for comparison
                    now_utc = datetime.now(timezone.utc)
                    last_used_utc = fc.last_used.replace(tzinfo=timezone.utc) if fc.last_used.tzinfo is None else fc.last_used
                    time_delta = now_utc - last_used_utc
                    if time_delta.total_seconds() < 60:
                        time_str = "just now"
                    elif time_delta.total_seconds() < 3600:
                        minutes = int(time_delta.total_seconds() / 60)
                        time_str = f"{minutes}m ago"
                    elif time_delta.total_seconds() < 86400:
                        hours = int(time_delta.total_seconds() / 3600)
                        time_str = f"{hours}h ago"
                    else:
                        days = int(time_delta.total_seconds() / 86400)
                        time_str = f"{days}d ago"

                    context_lines.append(
                        f"- {fc.file_path} ({fc.context_type} {fc.use_count}x, {time_str})"
                    )

                if context_lines:
                    context_summary = "\n".join(context_lines)
                    prompt = f"{prompt}\n\n[Recently accessed files in this session:]\n{context_summary}"
                    logger.info(f"Enhanced prompt with {len(relevant_files)} file context(s)")

        except Exception as e:
            logger.warning(f"Failed to enhance prompt with file context: {e}")

        # Create command history entry
        cmd_history = await deps.db.add_command(session.id, prompt)
        await deps.db.update_command_status(cmd_history.id, "running")

        # Send initial processing message (in thread if applicable)
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,  # Reply in thread if this is a thread message
            text=f"Processing: {prompt[:100]}...",  # Fallback for notifications
            blocks=SlackFormatter.processing_message(prompt),
        )
        message_ts = response["ts"]

        # Execute command with streaming updates
        accumulated_output = ""
        accumulated_tools: dict[str, "ToolActivity"] = {}  # tool_id -> ToolActivity
        last_update_time = 0
        execution_id = str(uuid.uuid4())

        # Import here to avoid circular imports
        from src.claude.streaming import ToolActivity

        async def on_chunk(msg):
            nonlocal accumulated_output, last_update_time

            # Accumulate text content
            if msg.type == "assistant" and msg.content:
                # Limit accumulated output to prevent memory issues
                if len(accumulated_output) < config.timeouts.streaming.max_accumulated_size:
                    accumulated_output += msg.content

            # Track tool activities
            if msg.tool_activities:
                for tool in msg.tool_activities:
                    if tool.id in accumulated_tools:
                        # Update existing tool with result
                        existing = accumulated_tools[tool.id]
                        if tool.result is not None:
                            existing.result = tool.result
                            existing.full_result = tool.full_result
                            existing.is_error = tool.is_error
                            existing.duration_ms = tool.duration_ms
                    else:
                        # Add new tool
                        accumulated_tools[tool.id] = tool

            # Rate limit updates to avoid Slack API limits
            current_time = asyncio.get_running_loop().time()
            if current_time - last_update_time > config.timeouts.slack.message_update_throttle:
                last_update_time = current_time
                try:
                    # Get tool list (most recent last)
                    tool_list = list(accumulated_tools.values())

                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=accumulated_output[:100] + "..." if len(accumulated_output) > 100 else accumulated_output,
                        blocks=SlackFormatter.streaming_update(
                            prompt,
                            accumulated_output,
                            tool_activities=tool_list,
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update message: {e}")

        try:
            result = await executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=channel_id,
                resume_session_id=session.claude_session_id,  # Resume previous session if exists
                execution_id=execution_id,
                on_chunk=on_chunk,
                db_session_id=session.id,  # Pass for smart context tracking
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

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
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=output[:100] + "..." if len(output) > 100 else output,
                    blocks=blocks,
                )
                # Upload files as separate messages
                try:
                    # Upload summary file
                    await client.files_upload_v2(
                        channel=channel_id,
                        content=file_content,
                        filename=file_title,
                        title="Claude Summary",
                        initial_comment="📄 Response summary",
                        filetype="text",
                    )
                    # Upload full detailed output file if available
                    if result.detailed_output and result.detailed_output != output:
                        raw_output_filename = f"claude_detailed_{cmd_history.id}.txt"
                        await client.files_upload_v2(
                            channel=channel_id,
                            content=result.detailed_output,
                            filename=raw_output_filename,
                            title="Claude Detailed Output",
                            initial_comment="📋 Complete response with tool use and results",
                            filetype="text",
                        )
                except Exception as upload_error:
                    logger.error(f"Failed to upload file: {upload_error}")
                    error_msg = str(upload_error)
                    if "missing_scope" in error_msg and "files:write" in error_msg:
                        await client.chat_postMessage(
                            channel=channel_id,
                            text="⚠️ Could not upload file: Missing `files:write` scope. Please add this scope in your Slack app configuration (OAuth & Permissions).",
                        )
                    else:
                        await client.chat_postMessage(
                            channel=channel_id,
                            text=f"⚠️ Could not upload file: {error_msg}",
                        )
            else:
                await client.chat_update(
                    channel=channel_id,
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
            logger.error(f"Error executing command: {e}\n{traceback.format_exc()}")
            await deps.db.update_command_status(
                cmd_history.id, "failed", error_message=str(e)
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=SlackFormatter.error_message(str(e)),
            )

    # Start Socket Mode handler
    handler = AsyncSocketModeHandler(app, config.SLACK_APP_TOKEN)

    # Setup shutdown handler
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info("Starting Slack Claude Code Bot...")
    logger.info(f"Default working directory: {config.DEFAULT_WORKING_DIR}")
    logger.info(f"Command timeout: {config.timeouts.execution.command}s")
    logger.info(f"Session idle timeout: {config.timeouts.pty.idle}s")

    # Start the handler
    await handler.connect_async()
    logger.info("Connected to Slack")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Cleanup
    await shutdown(executor)
    await handler.close_async()


if __name__ == "__main__":
    asyncio.run(main())
