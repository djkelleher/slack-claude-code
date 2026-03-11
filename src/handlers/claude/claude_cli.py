"""Claude CLI passthrough command handlers."""

import asyncio
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import (
    normalize_codex_approval_mode,
)
from src.config import (
    config,
)
from src.handlers.backend_command_adapter import (
    format_codex_review_status,
    get_codex_mcp_summary,
    unsupported_claude_slash_command_message,
)
from src.utils.execution_scope import build_session_scope
from src.utils.formatters.command import command_response_with_tables, error_message
from src.utils.formatters.streaming import processing_message
from src.utils.model_selection import (
    backend_label_for_model,
    codex_model_validation_error,
    get_all_model_options,
    get_claude_model_options,
    get_codex_model_options,
    model_display_name,
    normalize_current_model,
    normalize_model_name,
)

from ..base import CommandContext, HandlerDependencies, slack_command


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

    async def _cancel_executor_operations(
        executor,
        ctx: CommandContext,
    ) -> int:
        """Cancel operations for the current scope when possible."""
        if not executor:
            return 0
        if ctx.thread_ts:
            session_scope = build_session_scope(ctx.channel_id, ctx.thread_ts)
            return await executor.cancel_by_scope(session_scope)
        return await executor.cancel_by_channel(ctx.channel_id)

    async def _cancel_codex_operations(
        ctx: CommandContext,
        deps: HandlerDependencies,
    ) -> int:
        """Cancel active Codex operations for this channel/thread."""
        return await _cancel_executor_operations(deps.codex_executor, ctx)

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
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        command_name = claude_command.strip().split(" ", 1)[0]
        unsupported_hint = unsupported_claude_slash_command_message(session, command_name)
        if unsupported_hint:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"{command_name} is not supported for Codex sessions.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":warning: `{command_name}` is Claude-specific and not available "
                                "for Codex sessions.\n\n"
                                f"{unsupported_hint}"
                            ),
                        },
                    },
                ],
            )
            return

        # Send processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Running: {claude_command}",
            blocks=processing_message(claude_command),
        )
        message_ts = response["ts"]

        try:
            result = await deps.executor.execute(
                prompt=claude_command,
                working_directory=session.working_directory,
                session_id=build_session_scope(ctx.channel_id, ctx.thread_ts),
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                model=session.model,
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
            )

            # Update session if needed
            if result.session_id:
                await deps.db.update_session_claude_id(
                    ctx.channel_id, ctx.thread_ts, result.session_id
                )

            output = result.output or result.error or ""
            if not output and result.detailed_output:
                output = result.detailed_output
            if not output:
                output = "Command completed (no output)"

            # Format response with table support (may produce multiple messages)
            message_blocks_list = command_response_with_tables(
                prompt=claude_command,
                output=output,
                command_id=None,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                is_error=not result.success,
            )

            # Update the first message
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=output[:100] + "..." if len(output) > 100 else output,
                blocks=message_blocks_list[0],
            )

            # Post additional messages for tables
            for blocks in message_blocks_list[1:]:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Table",
                    blocks=blocks,
                )

        except Exception as e:
            ctx.logger.error(f"Claude CLI command failed: {e}")
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=error_message(str(e)),
            )

    @app.command("/clear")
    @slack_command()
    async def handle_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /clear command - cancel processes and reset conversation sessions."""
        # Step 1: Cancel/stop active executions for this channel
        cancelled_count = await _cancel_executor_operations(deps.executor, ctx)
        cancelled_count += await _cancel_codex_operations(ctx, deps)

        # Brief wait for graceful shutdown
        if cancelled_count > 0:
            await asyncio.sleep(0.5)

        # Step 2: Clear backend session IDs so next message starts fresh
        await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)
        ctx.logger.info("Cleared Claude and Codex session IDs")

        # Note: We don't send /clear to Claude CLI because it only works in
        # interactive mode, not with -p flag. Clearing the session ID above
        # is sufficient - the next message will start a new conversation.

        # Step 3: Notify user
        if cancelled_count > 0:
            message = f"Cancelled {cancelled_count} active process(es) and cleared conversation."
        else:
            message = "Conversation cleared. Your next message will start a fresh session."

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=message,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f":white_check_mark: {message}"},
                }
            ],
        )

    @app.command("/esc")
    @slack_command()
    async def handle_esc(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /esc command - interrupt current operation (like pressing Escape)."""
        # Interrupt all active executions for this channel
        cancelled_count = await _cancel_executor_operations(deps.executor, ctx)
        cancelled_count += await _cancel_codex_operations(ctx, deps)

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
        directory = ctx.text.strip()

        # Resolve and validate path
        resolved_dir = Path(directory).expanduser().resolve()
        if not resolved_dir.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Path does not exist: {resolved_dir}",
                blocks=error_message(f"Path does not exist: `{resolved_dir}`"),
            )
            return
        if not resolved_dir.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Not a directory: {resolved_dir}",
                blocks=error_message(f"Not a directory: `{resolved_dir}`"),
            )
            return

        await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        # Add resolved directory to session's added_dirs list
        added_dirs = await deps.db.add_session_dir(ctx.channel_id, ctx.thread_ts, str(resolved_dir))

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Added directory: {resolved_dir}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directory Added*\n\n"
                            f"Added `{resolved_dir}` to context.\n\n"
                            f"*Current directories ({len(added_dirs)}):*\n"
                            + "\n".join(f"• `{d}`" for d in added_dirs)
                        ),
                    },
                }
            ],
        )

    @app.command("/remove-dir")
    @slack_command(require_text=True, usage_hint="Usage: /remove-dir <path>")
    async def handle_remove_dir(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /remove-dir <path> command - remove directory from context."""
        directory = ctx.text.strip()

        # Get current dirs to check if it exists
        current_dirs = await deps.db.get_session_dirs(ctx.channel_id, ctx.thread_ts)

        if directory not in current_dirs:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"Directory not found: {directory}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":warning: Directory `{directory}` is not in the context.\n\n"
                                f"*Current directories ({len(current_dirs)}):*\n"
                                + (
                                    "\n".join(f"• `{d}`" for d in current_dirs)
                                    if current_dirs
                                    else "_No directories added_"
                                )
                            ),
                        },
                    }
                ],
            )
            return

        # Remove directory from session's added_dirs list
        remaining_dirs = await deps.db.remove_session_dir(ctx.channel_id, ctx.thread_ts, directory)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Removed directory: {directory}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directory Removed*\n\n"
                            f"Removed `{directory}` from context.\n\n"
                            f"*Remaining directories ({len(remaining_dirs)}):*\n"
                            + (
                                "\n".join(f"• `{d}`" for d in remaining_dirs)
                                if remaining_dirs
                                else "_No directories added_"
                            )
                        ),
                    },
                }
            ],
        )

    @app.command("/list-dirs")
    @slack_command()
    async def handle_list_dirs(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /list-dirs command - list directories in context."""
        added_dirs = await deps.db.get_session_dirs(ctx.channel_id, ctx.thread_ts)

        # Get working directory for context
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Directories in context: {len(added_dirs)}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directories in Context*\n\n"
                            f"*Working directory:* `{session.working_directory}`\n\n"
                            f"*Added directories ({len(added_dirs)}):*\n"
                            + (
                                "\n".join(f"• `{d}`" for d in added_dirs)
                                if added_dirs
                                else "_No additional directories added_"
                            )
                            + "\n\n_Use `/add-dir <path>` to add directories, `/remove-dir <path>` to remove._"
                        ),
                    },
                }
            ],
        )

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

    @app.command("/usage")
    @slack_command()
    async def handle_usage(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /usage command - show Claude `/usage` and Codex `/status` output."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        session_scope = build_session_scope(ctx.channel_id, ctx.thread_ts)
        is_codex_backend = session.get_backend() == "codex"
        claude_model = session.model if not is_codex_backend else None
        codex_model = session.model if is_codex_backend else None

        async def _run_claude_usage() -> object:
            if not deps.executor:
                return RuntimeError("Claude executor is not configured.")
            return await deps.executor.execute(
                prompt="/usage",
                working_directory=session.working_directory,
                session_id=session_scope,
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                model=claude_model,
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
            )

        async def _run_codex_status() -> object:
            if not deps.codex_executor:
                return RuntimeError("Codex executor is not configured.")
            return await deps.codex_executor.execute(
                prompt="/status",
                working_directory=session.working_directory,
                session_id=session_scope,
                resume_session_id=session.codex_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                sandbox_mode=session.sandbox_mode,
                approval_mode=session.approval_mode,
                model=codex_model,
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
            )

        def _format_backend_output(
            label: str, command_name: str, result: object
        ) -> tuple[bool, str]:
            if isinstance(result, Exception):
                return False, f"{label} {command_name} failed: {result}"

            success = bool(result.success)
            output = result.output or result.error or ""
            if not output and result.detailed_output:
                output = result.detailed_output
            if not output:
                output = "Command completed (no output)."

            header = f"{label} {command_name} ({'ok' if success else 'error'})"
            return success, f"{header}\n{output}"

        claude_result, codex_result = await asyncio.gather(
            _run_claude_usage(),
            _run_codex_status(),
            return_exceptions=True,
        )

        if not isinstance(claude_result, Exception) and claude_result.session_id:
            await deps.db.update_session_claude_id(
                ctx.channel_id, ctx.thread_ts, claude_result.session_id
            )
        if not isinstance(codex_result, Exception) and codex_result.session_id:
            await deps.db.update_session_codex_id(
                ctx.channel_id, ctx.thread_ts, codex_result.session_id
            )

        claude_success, claude_output = _format_backend_output("Claude", "/usage", claude_result)
        codex_success, codex_output = _format_backend_output("Codex", "/status", codex_result)
        combined_output = f"{claude_output}\n\n{codex_output}"
        combined_success = claude_success and codex_success
        blocks_list = command_response_with_tables(
            prompt="/usage",
            output=combined_output,
            command_id=None,
            is_error=not combined_success,
        )

        for index, blocks in enumerate(blocks_list):
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Usage" if index == 0 else "Usage (continued)",
                blocks=blocks,
            )

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
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        if ctx.text:
            # Direct model selection via command argument
            model_name = ctx.text.strip().lower()
            normalized = normalize_model_name(model_name)
            validation_error = codex_model_validation_error(normalized)
            if validation_error:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text=f"Unsupported Codex model: {normalized}",
                    blocks=error_message(validation_error),
                )
                return

            await deps.db.update_session_model(ctx.channel_id, ctx.thread_ts, normalized)

            backend_label = backend_label_for_model(normalized)
            selected_display = model_display_name(normalized)
            model_id_line = ""
            if normalized and selected_display != normalized:
                model_id_line = f"\n_Model ID: `{normalized}`_"
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":heavy_check_mark: Model changed to *{selected_display}* ({backend_label})",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":heavy_check_mark: Model changed to *{selected_display}*"
                                f"{model_id_line}\n_Backend: {backend_label}_"
                            ),
                        },
                    }
                ],
            )
        else:
            # Show current model and allow selection via buttons
            normalized_current_model = normalize_current_model(session.model)
            current_backend = backend_label_for_model(normalized_current_model)

            # Available models (organized by backend)
            claude_models = get_claude_model_options()
            codex_models = get_codex_model_options()

            # Get display name for current model
            all_models = get_all_model_options()
            current_display = next(
                (m["display"] for m in all_models if m["value"] == normalized_current_model),
                model_display_name(normalized_current_model),
            )

            # Build button blocks
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Current Model:* {current_display}\n"
                            f"*Backend:* {current_backend}\n\nSelect a model:"
                        ),
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Claude Code Models*"},
                },
            ]

            for model in claude_models:
                is_current = model["value"] == normalized_current_model
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

            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*OpenAI Codex Models*"},
                }
            )

            for model in codex_models:
                is_current = model["value"] == normalized_current_model
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

            # Add custom model option
            blocks.append({"type": "divider"})

            # Check if current model is a custom one (not in predefined lists)
            predefined_models = {m["value"] for m in all_models}
            is_custom_model = normalized_current_model not in predefined_models

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Custom Model*\nEnter any model ID (e.g., `claude-sonnet-4-6[1m]` or `gpt-5.3-codex-extra-high`)"
                            + (
                                f"\n_Currently using: `{normalized_current_model}`_"
                                if is_custom_model
                                else ""
                            )
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Enter Custom Model",
                            "emoji": True,
                        },
                        "action_id": "select_model_custom",
                        "value": f"{ctx.channel_id}|{ctx.thread_ts or ''}",
                    },
                }
            )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current model: {current_display}",
                blocks=blocks,
            )

    @app.command("/init")
    @slack_command()
    async def handle_init(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /init command - initialize project with CLAUDE.md."""
        await _send_claude_command(ctx, "/init", deps)

    @app.command("/review")
    @slack_command()
    async def handle_review(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /review command - request code review."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            if not deps.codex_executor:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex executor is not configured.",
                    blocks=error_message("Codex executor is not configured."),
                )
                return
            if not session.codex_session_id:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="No active Codex session.",
                    blocks=error_message(
                        "No active Codex thread for this session yet. Send a Codex message first."
                    ),
                )
                return

            tokens = ctx.text.split() if ctx.text else []
            if tokens and tokens[0].lower() in {"status", "read"}:
                thread_arg = tokens[1] if len(tokens) > 1 else "current"
                thread_id = (
                    session.codex_session_id if thread_arg == "current" else thread_arg.strip()
                )
                if not thread_id:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="No active Codex session.",
                        blocks=error_message(
                            "No active Codex thread for this session yet. Send a Codex message first."
                        ),
                    )
                    return
                try:
                    result = await deps.codex_executor.thread_read(
                        thread_id=thread_id,
                        working_directory=session.working_directory,
                        include_turns=True,
                    )
                    thread = result.get("thread", {})
                    summary = format_codex_review_status(thread, thread_id)
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="Codex review status",
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": summary},
                            }
                        ],
                    )
                except Exception as e:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text=f"Failed to fetch review status: {e}",
                        blocks=error_message(str(e)),
                    )
                return

            target: dict
            if ctx.text:
                target = {"type": "custom", "instructions": ctx.text}
            else:
                target = {"type": "uncommittedChanges"}

            try:
                result = await deps.codex_executor.review_start(
                    thread_id=session.codex_session_id,
                    target=target,
                    working_directory=session.working_directory,
                )
                review_thread_id = result.get("reviewThreadId")
                turn = result.get("turn", {})
                turn_id = turn.get("id", "unknown")
                review_summary = (
                    f":mag: Started Codex review for thread `{session.codex_session_id}`.\n"
                    f"Turn: `{turn_id}`"
                )
                if review_thread_id:
                    review_summary += f"\nReview thread: `{review_thread_id}`"
                    review_summary += (
                        f"\nUse `/review status {review_thread_id}` to inspect progress."
                    )
                else:
                    review_summary += "\nUse `/review status` to inspect latest turn status."
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex review started",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": review_summary},
                        }
                    ],
                )
            except Exception as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Failed to start review: {e}",
                    blocks=error_message(str(e)),
                )
            return
        await _send_claude_command(ctx, "/review", deps)

    @app.command("/permissions")
    @slack_command()
    async def handle_permissions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /permissions command - view or update permissions."""
        # Note: /permissions only works in Claude CLI interactive mode, not with -p flag.
        # In print mode, slash commands get interpreted as skill invocations.
        # Show info about how to manage permissions in Slack mode.
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            current_approval = normalize_codex_approval_mode(
                session.approval_mode or config.CODEX_APPROVAL_MODE
            )
            current_sandbox = session.sandbox_mode or config.CODEX_SANDBOX_MODE
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Codex permission settings",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                ":lock: *Codex Permissions*\n\n"
                                f"*Approval mode:* `{current_approval}`\n"
                                f"*Sandbox mode:* `{current_sandbox}`\n\n"
                                "Use:\n"
                                "• `/mode approval <mode>` to control approvals\n"
                                "• `/mode sandbox <mode>` to control filesystem access\n"
                                "• `/mode bypass|ask|default|plan` for compatibility session mode"
                            ),
                        },
                    }
                ],
            )
            return

        current_mode = session.permission_mode or "default"

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Permission settings",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":lock: *Permissions*\n\n"
                            f"*Current mode:* `{current_mode}`\n\n"
                            "Use `/mode` to change permission modes:\n"
                            "• `/mode ask` - Ask for approval on sensitive operations\n"
                            "• `/mode plan` - Plan-only mode (no execution)\n"
                            "• `/mode accept` - Auto-approve file edits\n"
                            "• `/mode bypass` - Skip all permission checks"
                        ),
                    },
                }
            ],
        )

    @app.command("/mcp")
    @slack_command()
    async def handle_mcp(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mcp command - show MCP server configuration."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            if not deps.codex_executor:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex executor is not configured.",
                    blocks=error_message("Codex executor is not configured."),
                )
                return
            try:
                summary = await get_codex_mcp_summary(
                    deps.codex_executor,
                    session.working_directory,
                )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex MCP status",
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}],
                )
            except Exception as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Failed to load MCP status: {e}",
                    blocks=error_message(str(e)),
                )
            return
        if ctx.text:
            await _send_claude_command(ctx, f"/mcp {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/mcp", deps)
