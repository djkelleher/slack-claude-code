"""Basic command handlers: /!, /cd, /diff, /h, /hist, /ls, /pwd."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.database.models import CommandHistory
from src.handlers.response_delivery import deliver_command_response
from src.utils.formatters.command import error_message
from src.utils.formatters.directory import cwd_updated, directory_listing
from src.utils.formatters.streaming import processing_message

from .base import CommandContext, HandlerDependencies, slack_command

_DEFAULT_HISTORY_SPAN = 10
_MAX_HISTORY_SPAN = 20
_SINGLE_HISTORY_PROMPT_LIMIT = 2400
_RANGE_HISTORY_PROMPT_LIMIT = 320


def _parse_history_selection(raw_text: str) -> tuple[int, int]:
    """Parse optional `/hist` text into 1-based inclusive history indexes."""
    text = (raw_text or "").strip()
    if not text:
        return 1, _DEFAULT_HISTORY_SPAN

    if ":" in text:
        start_text, end_text = text.split(":", maxsplit=1)
        start_text = start_text.strip()
        end_text = end_text.strip()
        if not start_text or not end_text:
            raise ValueError("Usage: /hist [N|N:M]")
        try:
            start = int(start_text)
            end = int(end_text)
        except ValueError as exc:
            raise ValueError("History indexes must be positive integers.") from exc
    else:
        try:
            start = int(text)
        except ValueError as exc:
            raise ValueError("History indexes must be positive integers.") from exc
        end = start

    if start < 1 or end < 1:
        raise ValueError("History indexes must be positive integers.")
    if end < start:
        raise ValueError("History range end must be greater than or equal to the start.")
    if (end - start + 1) > _MAX_HISTORY_SPAN:
        raise ValueError(f"History range too large. Maximum span is {_MAX_HISTORY_SPAN} prompts.")
    return start, end


def _format_history_timestamp(created_at: datetime) -> str:
    """Render a stable UTC timestamp for prompt history entries."""
    value = created_at
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _truncate_history_prompt(prompt: str, limit: int) -> str:
    """Return literal prompt text truncated for Slack display."""
    normalized = (prompt or "").strip() or "(empty prompt)"
    if len(normalized) > limit:
        normalized = normalized[: limit - 3].rstrip() + "..."
    return normalized


def _history_entry_blocks(
    entries: list[CommandHistory],
    *,
    start_index: int,
) -> list[dict]:
    """Build Slack blocks for one or more prompt history entries."""
    blocks: list[dict] = []
    prompt_limit = (
        _SINGLE_HISTORY_PROMPT_LIMIT if len(entries) == 1 else _RANGE_HISTORY_PROMPT_LIMIT
    )

    for offset, entry in enumerate(entries):
        history_index = start_index + offset
        prompt_text = _truncate_history_prompt(entry.command, prompt_limit)
        blocks.append(
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {
                                "type": "text",
                                "text": (
                                    f"#{history_index} | {entry.status} | "
                                    f"{_format_history_timestamp(entry.created_at)}"
                                ),
                            }
                        ],
                    },
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": prompt_text}],
                    },
                ],
            }
        )
        if offset != len(entries) - 1:
            blocks.append({"type": "divider"})

    return blocks


def _prompt_history_blocks(
    entries: list[CommandHistory],
    *,
    start_index: int,
    requested_end_index: int,
    total: int,
) -> list[dict]:
    """Format prompt history results for Slack."""
    actual_end_index = start_index + len(entries) - 1
    label = "prompt" if actual_end_index == start_index else "prompts"
    header_text = (
        f"*Prompt History*\nShowing {label} #{start_index}"
        if actual_end_index == start_index
        else f"*Prompt History*\nShowing prompts #{start_index}-#{actual_end_index}"
    )
    header_text += f" of {total} (most recent is #1)"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        },
        {"type": "divider"},
    ]

    if actual_end_index < requested_end_index:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_Requested through #{requested_end_index}, "
                            f"but only {total} prompt(s) exist in this session._"
                        ),
                    }
                ],
            }
        )
        blocks.append({"type": "divider"})

    blocks.extend(_history_entry_blocks(entries, start_index=start_index))
    return blocks


def _diff_entry_summary(entry: CommandHistory) -> str:
    """Return human-readable diff summary for one prompt history entry."""
    return entry.git_diff_summary or "No new commits recorded for this prompt."


def _prompt_diff_blocks(
    entries: list[CommandHistory],
    *,
    start_index: int,
    requested_end_index: int,
    total: int,
) -> list[dict]:
    """Format prompt-scoped git diff snapshot summaries for Slack."""
    actual_end_index = start_index + len(entries) - 1
    label = "prompt" if actual_end_index == start_index else "prompts"
    header_text = (
        f"*Prompt Diff History*\nShowing {label} #{start_index}"
        if actual_end_index == start_index
        else f"*Prompt Diff History*\nShowing prompts #{start_index}-#{actual_end_index}"
    )
    header_text += f" of {total} (most recent is #1)"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        },
        {"type": "divider"},
    ]

    if actual_end_index < requested_end_index:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_Requested through #{requested_end_index}, "
                            f"but only {total} prompt(s) exist in this session._"
                        ),
                    }
                ],
            }
        )
        blocks.append({"type": "divider"})

    for offset, entry in enumerate(entries):
        history_index = start_index + offset
        prompt_text = _truncate_history_prompt(entry.command, _RANGE_HISTORY_PROMPT_LIMIT)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*#{history_index}* | {entry.status} | "
                        f"{_format_history_timestamp(entry.created_at)}\n"
                        f"{_diff_entry_summary(entry)}"
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": prompt_text}],
                    }
                ],
            }
        )
        if offset != len(entries) - 1:
            blocks.append({"type": "divider"})

    return blocks


def _build_prompt_diff_file_content(
    entries: list[CommandHistory],
    *,
    start_index: int,
) -> str:
    """Build combined prompt-scoped diff snapshot text for file upload."""
    sections: list[str] = []

    for offset, entry in enumerate(entries):
        history_index = start_index + offset
        body = entry.git_diff_output or "No new commits recorded for this prompt."
        sections.append(
            "\n".join(
                [
                    f"# Prompt #{history_index}",
                    f"status: {entry.status}",
                    f"created_at: {_format_history_timestamp(entry.created_at)}",
                    "prompt:",
                    entry.command or "(empty prompt)",
                    "",
                    f"summary: {_diff_entry_summary(entry)}",
                    "",
                    body,
                ]
            )
        )

    return "\n\n".join(sections)


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
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
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
            db=deps.db,
            terminal_style=True,
        )

    @app.command("/hist")
    @app.command("/h")
    @slack_command()
    async def handle_history(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle `/hist [N|N:M]` by showing recent prompt history for the session."""
        try:
            start_index, end_index = _parse_history_selection(ctx.text)
        except ValueError as exc:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=str(exc),
                blocks=error_message(str(exc)),
            )
            return

        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        limit = end_index - start_index + 1
        history, total = await deps.db.get_prompt_history(
            session.id,
            limit=limit,
            offset=start_index - 1,
        )

        if total == 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="No prompt history yet for this session.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "_No prompt history yet for this session._",
                        },
                    }
                ],
            )
            return

        if not history:
            message = f"History index out of range. This session has {total} prompt(s)."
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=message,
                blocks=error_message(message),
            )
            return

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Prompt history",
            blocks=_prompt_history_blocks(
                history,
                start_index=start_index,
                requested_end_index=end_index,
                total=total,
            ),
        )

    @app.command("/diff")
    @slack_command()
    async def handle_diff(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle `/diff [N|N:M]` by showing prompt-scoped git diff snapshots."""
        try:
            start_index, end_index = _parse_history_selection(ctx.text)
        except ValueError as exc:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=str(exc),
                blocks=error_message(str(exc)),
            )
            return

        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        limit = end_index - start_index + 1
        history, total = await deps.db.get_prompt_history(
            session.id,
            limit=limit,
            offset=start_index - 1,
        )

        if total == 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="No prompt history yet for this session.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "_No prompt history yet for this session._",
                        },
                    }
                ],
            )
            return

        if not history:
            message = f"History index out of range. This session has {total} prompt(s)."
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=message,
                blocks=error_message(message),
            )
            return

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Prompt diff history",
            blocks=_prompt_diff_blocks(
                history,
                start_index=start_index,
                requested_end_index=end_index,
                total=total,
            ),
        )

        if not any(entry.git_diff_output for entry in history):
            return

        file_label = (
            f"prompt-{start_index}"
            if len(history) == 1
            else f"prompts-{start_index}-{start_index + len(history) - 1}"
        )
        await ctx.client.files_upload_v2(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            content=_build_prompt_diff_file_content(history, start_index=start_index),
            filename=f"git-diff-{file_label}.diff",
            title=(
                f"Git diff for prompt #{start_index}"
                if len(history) == 1
                else (f"Git diffs for prompts #{start_index}-" f"{start_index + len(history) - 1}")
            ),
            initial_comment=(
                "Prompt-scoped git commit diff snapshot"
                if len(history) == 1
                else "Prompt-scoped git commit diff snapshots"
            ),
        )

    @app.command("/ls")
    @slack_command()
    async def handle_ls(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /ls [path] command - list directory contents and show cwd."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
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
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
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
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f":file_folder: Current working directory: `{session.working_directory}`",
        )
