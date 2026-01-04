#!/usr/bin/env python3
"""
Slack Claude Code Bot - Main Application Entry Point

A Slack app that allows running Claude Code CLI commands from Slack,
with each channel representing a separate persistent PTY session.
"""

import asyncio
import logging
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
from src.handlers import register_commands, register_actions
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
    executor = SubprocessExecutor()  # Uses config.timeouts.execution.command

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
        prompt = event.get("text", "").strip()

        if not prompt:
            logger.debug("Empty prompt, ignoring")
            return

        # Get or create session
        session = await deps.db.get_or_create_session(
            channel_id, config.DEFAULT_WORKING_DIR
        )

        # Create command history entry
        cmd_history = await deps.db.add_command(session.id, prompt)
        await deps.db.update_command_status(cmd_history.id, "running")

        # Send initial processing message
        response = await client.chat_postMessage(
            channel=channel_id,
            text=f"Processing: {prompt[:100]}...",  # Fallback for notifications
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
                        await client.chat_update(
                            channel=channel_id,
                            ts=message_ts,
                            text=accumulated_output[:100] + "..." if len(accumulated_output) > 100 else accumulated_output,
                            blocks=SlackFormatter.streaming_update(
                                prompt, accumulated_output
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
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, result.session_id)

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
                # Upload file
                await client.files_upload_v2(
                    channel=channel_id,
                    content=file_content,
                    filename=file_title,
                    title=file_title,
                    thread_ts=message_ts,
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
