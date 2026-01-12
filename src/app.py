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
from datetime import datetime, timezone

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
from src.utils.slack_helpers import post_text_snippet
from src.utils.streaming import StreamingMessageState
from src.utils.detail_cache import DetailCache
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


async def post_channel_notification(
    client,
    db: DatabaseRepository,
    channel_id: str,
    thread_ts: str | None,
    notification_type: str,
) -> None:
    """
    Post a brief notification to the channel (not thread) to trigger Slack sounds and unread badges.

    Args:
        client: Slack WebClient
        db: Database repository
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (for linking)
        notification_type: "completion" or "permission"
    """
    try:
        settings = await db.get_notification_settings(channel_id)

        if notification_type == "completion" and not settings.notify_on_completion:
            return
        elif notification_type == "permission" and not settings.notify_on_permission:
            return

        # Build thread link if we have a thread_ts
        if thread_ts:
            thread_link = f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
            if notification_type == "completion":
                message = f"✅ Claude finished • <{thread_link}|View thread>"
            else:
                message = f"⚠️ Claude needs permission • <{thread_link}|Respond in thread>"
        else:
            if notification_type == "completion":
                message = "✅ Claude finished"
            else:
                message = "⚠️ Claude needs permission"

        # Post to channel (NOT to thread) - this triggers sound + unread badge
        await client.chat_postMessage(
            channel=channel_id,
            text=message,
        )
        logger.debug(f"Posted {notification_type} notification to channel {channel_id}")

    except Exception as e:
        # Don't fail the main operation if notification fails
        logger.warning(f"Failed to post channel notification: {e}")


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

        # Setup streaming state with tool tracking
        execution_id = str(uuid.uuid4())
        streaming_state = StreamingMessageState(
            channel_id=channel_id,
            message_ts=message_ts,
            prompt=prompt,
            client=client,
            logger=logger,
            track_tools=True,
        )
        pending_question = None  # Track if we detect an AskUserQuestion

        # Import here to avoid circular imports
        from src.question import QuestionManager

        async def on_chunk(msg):
            nonlocal pending_question

            # Detect AskUserQuestion tool before updating state
            if msg.tool_activities:
                for tool in msg.tool_activities:
                    logger.debug(f"Tool activity: {tool.name} (id={tool.id[:8]}..., result={'has result' if tool.result else 'None'})")
                    if tool.name == "AskUserQuestion" and tool.result is None:
                        if tool.id not in streaming_state.tool_activities:
                            # Create pending question when we first see the tool
                            pending_question = await QuestionManager.create_pending_question(
                                session_id=str(session.id),
                                channel_id=channel_id,
                                thread_ts=thread_ts,
                                tool_use_id=tool.id,
                                tool_input=tool.input,
                            )
                            logger.info(f"Detected AskUserQuestion tool, created pending question {pending_question.question_id}")
                        else:
                            logger.debug(f"AskUserQuestion tool {tool.id[:8]}... already tracked, skipping")

            # Update streaming state
            content = msg.content if msg.type == "assistant" else ""
            tools = msg.tool_activities
            if content or tools:
                await streaming_state.append_and_update(content or "", tools)

        try:
            result = await executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=channel_id,
                resume_session_id=session.claude_session_id,  # Resume previous session if exists
                execution_id=execution_id,
                on_chunk=on_chunk,
                permission_mode=session.permission_mode,  # Use session's mode (falls back to config)
                db_session_id=session.id,  # Pass for smart context tracking
                model=session.model,  # Use session's selected model
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

            # Handle AskUserQuestion - loop to handle multiple questions
            while pending_question and result.session_id:
                logger.info(f"Claude asked a question, posting to Slack and waiting for response")

                # Update the main message to show Claude is waiting
                try:
                    text_preview = streaming_state.accumulated_output[:100] + "..." if len(streaming_state.accumulated_output) > 100 else streaming_state.accumulated_output
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=text_preview,
                        blocks=SlackFormatter.streaming_update(
                            prompt,
                            streaming_state.accumulated_output + "\n\n_Waiting for your response..._",
                            tool_activities=streaming_state.get_tool_list(),
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update message: {e}")

                # Post the question to Slack
                await QuestionManager.post_question_to_slack(
                    pending_question,
                    client,
                    deps.db,
                )

                # Wait for user to answer (no timeout)
                answers = await QuestionManager.wait_for_answer(
                    pending_question.question_id,
                )

                if answers:
                    # User answered - format and send as follow-up to Claude
                    answer_text = QuestionManager.format_answer_for_claude(pending_question)
                    logger.info(f"User answered question, sending to Claude: {answer_text[:100]}")

                    # Reset pending_question before continuing - on_chunk may set a new one
                    pending_question = None

                    # Continue the conversation with the user's answer
                    # This will resume the session
                    result = await executor.execute(
                        prompt=answer_text,
                        working_directory=session.working_directory,
                        session_id=channel_id,
                        resume_session_id=result.session_id,  # Resume the same session
                        execution_id=str(uuid.uuid4()),
                        on_chunk=on_chunk,  # Reuse the same chunk handler
                        permission_mode=session.permission_mode,
                        db_session_id=session.id,
                        model=session.model,
                    )

                    # Update session with new Claude session ID
                    if result.session_id:
                        await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)
                    # Loop continues - will check if pending_question was set by on_chunk
                else:
                    # Timeout or cancelled - update message and break
                    logger.info(f"Question timed out or cancelled")
                    result.output = streaming_state.accumulated_output + "\n\n_Question timed out - no response received._"
                    result.success = False
                    break

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
                # Post response content
                try:
                    # Post summary as inline snippet
                    await post_text_snippet(
                        client=client,
                        channel_id=channel_id,
                        content=file_content,
                        title="📄 Response summary",
                        thread_ts=thread_ts,
                    )
                    # Store detailed output in cache and post button to view it
                    if result.detailed_output and result.detailed_output != output:
                        DetailCache.store(cmd_history.id, result.detailed_output)
                        await client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=thread_ts,
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
                    logger.error(f"Failed to post snippet: {post_error}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"⚠️ Could not post detailed output: {str(post_error)[:100]}",
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
