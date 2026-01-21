"""Main Slack app entrypoint with real-time streaming updates."""

import asyncio
import signal
import sys
import traceback
from typing import Optional

from loguru import logger
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from src.approval.plan_manager import PlanApprovalManager
from src.claude.subprocess_executor import SubprocessExecutor
from src.config import config
from src.database.migrations import init_database
from src.database.repository import DatabaseRepository
from src.handlers import (
    register_agent_commands,
    register_basic_commands,
    register_claude_cli_commands,
    register_git_commands,
    register_mode_command,
    register_notifications_command,
    register_parallel_commands,
    register_queue_commands,
    register_session_commands,
)
from src.handlers.actions import register_actions
from src.handlers.base import HandlerDependencies
from src.question.manager import QuestionManager
from src.utils.file_downloader import download_slack_file
from src.utils.formatting import SlackFormatter
from src.utils.streaming import StreamingMessageState

async def startup() -> tuple[DatabaseRepository, SubprocessExecutor]:
    """Initialize database and executor."""
    await init_database(config.DATABASE_PATH)
    db = DatabaseRepository(config.DATABASE_PATH)
    executor = SubprocessExecutor()
    return db, executor


async def cleanup_executor(executor: SubprocessExecutor) -> None:
    """Cleanup resources on shutdown."""
    await executor.shutdown()


async def main() -> None:
    """Main entrypoint."""
    # Validate configuration
    errors = config.validate_required()
    if errors:
        for error in errors:
            logger.error(error)
        sys.exit(1)

    logger.info("Starting Slack Claude Code bot...")

    # Create Slack app
    app = AsyncApp(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    # Initialize resources
    db, executor = await startup()
    deps = HandlerDependencies(db=db, executor=executor)

    # Register all command handlers
    register_basic_commands(app, deps)
    register_claude_cli_commands(app, deps)
    register_mode_command(app, deps)
    register_notifications_command(app, deps)
    register_session_commands(app, deps)
    register_git_commands(app, deps)
    register_queue_commands(app, deps)
    register_parallel_commands(app, deps)
    register_agent_commands(app, deps)

    # Register actions (button clicks, etc.)
    register_actions(app, deps)

    @app.event("message")
    async def handle_message(client: AsyncWebClient, event: dict, logger):
        """Handle incoming messages and run Claude."""
        logger.info(f"Received message event: channel={event.get('channel')}, text={event.get('text', '')[:50]}")

        # Ignore bot messages and edited messages
        if event.get("subtype") in ("bot_message", "message_changed"):
            logger.debug(f"Ignoring message with subtype: {event.get('subtype')}")
            return

        # Get user ID and check for app mentions
        user_id = event.get("user")
        channel_id = event["channel"]
        channel_type = event.get("channel_type", "")
        text = event.get("text", "").strip()

        # Get thread_ts for thread-based sessions
        # If message is in a thread, use thread_ts
        # If message starts a thread, message ts becomes the thread identifier
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts")

        # Determine if we should respond:
        # 1. Direct messages (IMs) - always respond
        # 2. Mentions - respond when bot is mentioned
        # 3. Thread replies - respond to our own threads

        # Get bot user ID once for all checks
        bot_info = await app.client.auth_test()
        bot_id = bot_info.get("user_id")
        bot_mention = f"<@{bot_id}>"

        is_dm = channel_type == "im"
        is_mention = bot_mention in text if text else False
        is_thread_reply = thread_ts is not None and thread_ts != message_ts

        # For DMs, always process
        # For channels, need mention or be in a thread we started
        should_respond = False

        if is_dm:
            should_respond = True
        elif is_mention:
            should_respond = True
            # Remove the mention from the text
            text = text.replace(bot_mention, "").strip()
        elif is_thread_reply:
            # Check if bot participated in this thread
            try:
                result = await client.conversations_replies(channel=channel_id, ts=thread_ts)
                messages = result.get("messages", [])
                bot_participated = any(msg.get("user") == bot_id for msg in messages)
                should_respond = bot_participated
            except Exception as e:
                logger.warning(f"Failed to check thread participation: {e}")

        if not should_respond:
            return

        # Don't process empty messages
        if not text and not event.get("files"):
            return

        # Get or create session for this channel/thread
        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Handle file uploads
        files_info = event.get("files", [])
        uploaded_file_paths = []

        for file_info in files_info:
            file_id = file_info.get("id")
            file_name = file_info.get("name", "unnamed")
            file_url = file_info.get("url_private_download") or file_info.get("url_private")

            if not file_url:
                logger.warning(f"No download URL for file: {file_name}")
                continue

            try:
                local_path = await download_slack_file(
                    file_url=file_url,
                    filename=file_name,
                    session_id=session.id,
                    bot_token=config.SLACK_BOT_TOKEN,
                    max_size_mb=config.MAX_FILE_SIZE_MB,
                )

                # Track uploaded file in database
                await deps.db.add_uploaded_file(
                    session_id=session.id,
                    slack_file_id=file_id,
                    filename=file_name,
                    local_path=local_path,
                    mimetype=file_info.get("mimetype", ""),
                    size=file_info.get("size", 0),
                )

                uploaded_file_paths.append(local_path)
                logger.info(f"Downloaded file: {file_name} -> {local_path}")

            except Exception as e:
                logger.error(f"Failed to download file {file_name}: {e}")
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts or message_ts,
                    text=f"Failed to download file `{file_name}`: {str(e)}",
                )

        # Build prompt with file references
        prompt = text
        if uploaded_file_paths:
            file_refs = "\n".join(f"- {path}" for path in uploaded_file_paths)
            prompt = f"{text}\n\nAttached files:\n{file_refs}"

        if not prompt.strip():
            return

        # Add command to history
        cmd_history = await deps.db.add_command(session.id, prompt)

        # Determine reply thread
        # If user is in a thread, reply there
        # Otherwise start a new thread from their message
        reply_thread_ts = thread_ts or message_ts

        # Post initial "thinking" message
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=reply_thread_ts,
            text="Processing...",
            blocks=SlackFormatter.processing_message(prompt[:100] + "..." if len(prompt) > 100 else prompt),
        )
        message_ts = response["ts"]

        # Update session's thread_ts if this is a new thread
        if not thread_ts:
            thread_ts = reply_thread_ts

        # Determine permission mode (session override or default)
        permission_mode = session.permission_mode or config.CLAUDE_PERMISSION_MODE

        # Create streaming state
        streaming_state = StreamingMessageState(
            client=client,
            channel_id=channel_id,
            message_ts=message_ts,
            prompt=prompt,
            max_tools_display=config.timeouts.streaming.max_tools_display,
            update_interval=config.timeouts.slack.message_update_throttle,
            thread_ts=reply_thread_ts,
        )

        try:
            # Update command status
            await deps.db.update_command_status(cmd_history.id, "running")

            # Execute with streaming
            streaming_state.start_heartbeat()

            pending_question = None  # Track if we detect an AskUserQuestion

            # Factory function to create on_chunk callback with proper closures
            def create_on_chunk_callback(state: StreamingMessageState):
                async def on_chunk(msg):
                    nonlocal pending_question

                    # Detect AskUserQuestion tool before updating state
                    if msg.tool_activities:
                        for tool in msg.tool_activities:
                            logger.debug(
                                f"Tool activity: {tool.name} (id={tool.id[:8]}..., result={'has result' if tool.result else 'None'})"
                            )
                            if tool.name == "AskUserQuestion":
                                logger.info(
                                    f"AskUserQuestion detected: id={tool.id[:8]}, result={tool.result is not None}, "
                                    f"in_state={tool.id in state.tool_activities}, pending={pending_question is not None}"
                                )
                                if tool.result is None:
                                    if tool.id not in state.tool_activities:
                                        # Create pending question when we first see the tool
                                        pending_question = await QuestionManager.create_pending_question(
                                            session_id=str(session.id),
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            tool_use_id=tool.id,
                                            tool_input=tool.input,
                                        )
                                        logger.info(
                                            f"Created pending question {pending_question.question_id} for AskUserQuestion {tool.id[:8]}"
                                        )

                    await state.update(msg.text, msg.tool_activities)

                return on_chunk

            on_chunk = create_on_chunk_callback(streaming_state)

            result = await deps.executor.execute(
                prompt=prompt,
                working_directory=session.working_directory,
                session_id=f"{channel_id}:{thread_ts}",
                resume_session_id=session.claude_session_id,
                execution_id=f"{channel_id}:{message_ts}",
                on_chunk=on_chunk,
                permission_mode=permission_mode,
                db_session_id=session.id,
                model=session.model,
            )

            # Store Claude session ID for continuation
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

            # Handle AskUserQuestion - loop to handle multiple questions
            question_count = 0
            max_questions = config.timeouts.execution.max_questions_per_conversation
            if pending_question and not result.session_id:
                logger.error(
                    "AskUserQuestion detected but no session_id available - cannot handle question"
                )
            while pending_question and result.session_id and question_count < max_questions:
                question_count += 1
                logger.info("Claude asked a question, posting to Slack and waiting for response")

                # Update the main message to show Claude is waiting
                try:
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="Claude is asking a question...",
                        blocks=SlackFormatter.streaming_update(
                            prompt[:100] + "..." if len(prompt) > 100 else prompt,
                            result.output + "\n\n⏳ _Waiting for your answer below..._",
                            list(streaming_state.tool_activities.values()),
                            is_complete=False,
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update message with question state: {e}")

                # Post the question to Slack and wait for response
                questions_data = pending_question.tool_input.get("questions", [])
                user_response = await QuestionManager.post_and_wait(
                    pending=pending_question,
                    slack_client=client,
                    questions=questions_data,
                    timeout=config.timeouts.execution.plan_approval,
                )

                if user_response is None:
                    # Timed out or cancelled
                    logger.warning("Question timed out or was cancelled")
                    # Update the message to indicate timeout
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="Question timed out",
                        blocks=SlackFormatter.streaming_update(
                            prompt[:100] + "..." if len(prompt) > 100 else prompt,
                            result.output + "\n\n⏱️ _Question timed out - no response received._",
                            list(streaming_state.tool_activities.values()),
                            is_complete=True,
                        ),
                    )
                    break

                # Continue the conversation with the user's response
                logger.info(f"User responded: {user_response[:100]}...")

                # Reset pending_question for next iteration
                pending_question = None

                # Create new streaming state for continuation
                streaming_state = StreamingMessageState(
                    client=client,
                    channel_id=channel_id,
                    message_ts=message_ts,
                    prompt=prompt,
                    max_tools_display=config.timeouts.streaming.max_tools_display,
                    update_interval=config.timeouts.slack.message_update_throttle,
                    thread_ts=reply_thread_ts,
                )
                streaming_state.start_heartbeat()

                # Create fresh callback for new streaming state
                on_chunk = create_on_chunk_callback(streaming_state)

                # Resume Claude with the user's response
                continuation_prompt = user_response
                result = await deps.executor.execute(
                    prompt=continuation_prompt,
                    working_directory=session.working_directory,
                    session_id=f"{channel_id}:{thread_ts}",
                    resume_session_id=result.session_id,  # Resume from the same session
                    execution_id=f"{channel_id}:{message_ts}:q{question_count}",
                    on_chunk=on_chunk,
                    permission_mode=permission_mode,
                    db_session_id=session.id,
                    model=session.model,
                )

                streaming_state.stop_heartbeat()

                # Update Claude session ID after continuation
                if result.session_id:
                    await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

            # Handle ExitPlanMode (plan approval workflow)
            if result.has_pending_plan_approval:
                logger.info("ExitPlanMode detected, initiating plan approval workflow")

                # Retrieve the plan content from the plan file
                plan_content: Optional[str] = None
                if result.plan_file_path:
                    try:
                        with open(result.plan_file_path, "r") as f:
                            plan_content = f.read()
                        logger.info(f"Read plan from {result.plan_file_path}: {len(plan_content)} chars")
                    except Exception as e:
                        logger.error(f"Failed to read plan file {result.plan_file_path}: {e}")

                # Request approval via Slack
                approved = await PlanApprovalManager.request_approval(
                    session_id=f"{channel_id}:{thread_ts}",
                    channel_id=channel_id,
                    plan_content=plan_content or result.output,
                    thread_ts=reply_thread_ts,
                    slack_client=client,
                    timeout=config.timeouts.execution.plan_approval,
                    db=deps.db,
                    user_id=user_id,
                )

                if approved:
                    logger.info("Plan approved, updating session to bypass mode")
                    # Update session to bypass mode for implementation
                    await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)

                    # Update the streaming message to show approval
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="Plan approved! Proceeding with implementation...",
                        blocks=SlackFormatter.streaming_update(
                            prompt[:100] + "..." if len(prompt) > 100 else prompt,
                            result.output + "\n\n✅ Plan approved. Implementation will proceed.",
                            list(streaming_state.tool_activities.values()),
                            is_complete=True,
                        ),
                    )
                else:
                    logger.info("Plan not approved or timed out")
                    # Update message to show plan was not approved
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text="Plan not approved",
                        blocks=SlackFormatter.streaming_update(
                            prompt[:100] + "..." if len(prompt) > 100 else prompt,
                            result.output + "\n\n❌ Plan not approved. You can modify your request or provide more details.",
                            list(streaming_state.tool_activities.values()),
                            is_complete=True,
                        ),
                    )
                return

            # Final update with complete status
            streaming_state.stop_heartbeat()

            await deps.db.update_command_status(
                cmd_history.id,
                "completed" if result.success else "failed",
                output=result.output,
                error_message=result.error,
            )

            # Check if output is too long for Slack
            if SlackFormatter.should_attach_file(result.output):
                blocks, filename, content = SlackFormatter.command_response_with_file(
                    prompt=prompt[:100] + "..." if len(prompt) > 100 else prompt,
                    output=result.output,
                    command_id=cmd_history.id,
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    is_error=not result.success,
                )

                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"Completed (see file for full output)",
                    blocks=blocks,
                )

                # Upload as file
                await client.files_upload_v2(
                    channel=channel_id,
                    thread_ts=reply_thread_ts,
                    content=content,
                    filename=filename,
                    title="Full Output",
                )
            else:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"Completed: {result.output[:500]}",
                    blocks=SlackFormatter.command_response(
                        prompt=prompt[:100] + "..." if len(prompt) > 100 else prompt,
                        output=result.output,
                        command_id=cmd_history.id,
                        duration_ms=result.duration_ms,
                        cost_usd=result.cost_usd,
                        is_error=not result.success,
                    ),
                )

        except Exception as e:
            logger.error(f"Error executing command: {e}\n{traceback.format_exc()}")
            streaming_state.stop_heartbeat()  # Stop heartbeat on error
            # Clean up pending question if one was created
            if pending_question:
                QuestionManager.cancel(pending_question.question_id)
            await deps.db.update_command_status(cmd_history.id, "failed", error_message=str(e))
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=SlackFormatter.error_message(str(e)),
            )

    # Start Socket Mode handler
    handler = AsyncSocketModeHandler(app, config.SLACK_APP_TOKEN)

    # Setup shutdown handler using asyncio's signal handling
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    # Start the handler (connect_async returns after connecting, start_async blocks forever)
    await handler.connect_async()
    logger.info("Bot is ready!")

    try:
        # Wait for shutdown signal
        await shutdown_event.wait()
    finally:
        # Cleanup
        logger.info("Shutting down...")
        await cleanup_executor(executor)
        await handler.close_async()
        logger.info("Shutdown complete")


def run():
    # Check for subcommands (e.g., ccslack config ...)
    if len(sys.argv) > 1 and sys.argv[0].endswith(("ccslack", "ccslack.exe")):
        subcommand = sys.argv[1].lower()
        if subcommand == "config":
            # Forward to config CLI with remaining args
            sys.argv = sys.argv[1:]  # Remove 'ccslack' from argv
            from src.cli import run as config_run

            return config_run()

    asyncio.run(main())


if __name__ == "__main__":
    run()
