"""Shared Slack response delivery helpers for command execution paths."""

from typing import Any, Awaitable, Callable, Optional

from src.config import config
from src.git.service import GitError, GitService
from src.utils.detail_cache import DetailCache
from src.utils.formatters.command import (
    command_response_with_file,
    command_response_with_tables,
    should_attach_file,
)


def _maybe_add_copy_output_button(blocks: list[dict], command_id: int) -> list[dict]:
    """Append a copy-output action block when the message still has block capacity."""
    if len(blocks) >= config.SLACK_MAX_BLOCKS_PER_MESSAGE:
        return blocks

    return blocks + [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Copy Output",
                        "emoji": True,
                    },
                    "action_id": "copy_command_output",
                    "value": str(command_id),
                }
            ],
        }
    ]


def _build_git_diff_upload_content(
    *,
    status: Any,
    unstaged_diff: str,
    staged_diff: str,
) -> str:
    """Build a readable text payload for Slack diff uploads."""
    sections = [f"# Git status\n{status.summary()}"]

    if status.untracked:
        untracked_files = "\n".join(f"- {path}" for path in status.untracked)
        sections.append(
            "# Untracked files\n"
            "These files are not included in `git diff` until they are staged.\n"
            f"{untracked_files}"
        )

    if unstaged_diff and unstaged_diff != "(no changes)":
        sections.append(f"# Unstaged diff\n{unstaged_diff}")

    if staged_diff and staged_diff != "(no changes)":
        sections.append(f"# Staged diff\n{staged_diff}")

    return "\n\n".join(sections)


def _build_git_activity_upload_content(git_tool_events: list[dict[str, Any]]) -> str:
    """Build a readable text payload for raw git tool activity uploads."""
    sections = [f"# Git tool activity\nRecorded {len(git_tool_events)} git-related tool result(s)."]

    for index, event in enumerate(git_tool_events, start=1):
        lines = [f"## Event {index}"]
        kind = str(event.get("kind", "unknown"))
        lines.append(f"kind: {kind}")
        lines.append(f"tool_id: {event.get('tool_id', '')}")
        lines.append(f"tool_name: {event.get('tool_name', '')}")
        lines.append("status: ERROR" if event.get("is_error") else "status: OK")
        duration_ms = event.get("duration_ms")
        if duration_ms is not None:
            lines.append(f"duration_ms: {duration_ms}")

        if kind == "shell":
            lines.append("command:")
            lines.append(str(event.get("command", "")))
        else:
            lines.append(f"server: {event.get('server', '')}")
            lines.append(f"tool: {event.get('mcp_tool', '')}")

        result = str(event.get("result", "") or "").strip()
        if result:
            lines.append("result:")
            lines.append(result)

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


async def _maybe_upload_git_activity(
    *,
    client: Any,
    channel_id: str,
    thread_ts: Optional[str],
    command_id: int,
    git_tool_events: list[dict[str, Any]],
    logger: Any,
) -> None:
    """Upload raw git tool activity for a completed prompt run when available."""
    if not git_tool_events:
        return

    try:
        await client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            content=_build_git_activity_upload_content(git_tool_events),
            filename=f"git-activity-command-{command_id}.txt",
            title=f"Git activity for command #{command_id}",
            initial_comment="Raw git tool activity captured during prompt execution",
        )
    except Exception as e:
        logger.error(f"Failed to upload git activity for command {command_id}: {e}")


async def _maybe_upload_git_diff(
    *,
    client: Any,
    channel_id: str,
    thread_ts: Optional[str],
    command_id: int,
    working_directory: Optional[str],
    logger: Any,
) -> None:
    """Upload a git diff file for completed prompt runs when possible."""
    if not working_directory:
        return

    git_service = GitService()

    try:
        if not await git_service.validate_git_repo(working_directory):
            return

        status = await git_service.get_status(working_directory)
        if status.is_clean:
            return

        unstaged_diff = await git_service.get_diff(working_directory, max_size=500_000)
        staged_diff = await git_service.get_diff(working_directory, staged=True, max_size=500_000)
        content = _build_git_diff_upload_content(
            status=status,
            unstaged_diff=unstaged_diff,
            staged_diff=staged_diff,
        )

        await client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts,
            content=content,
            filename=f"git-diff-command-{command_id}.diff",
            title=f"Git diff for command #{command_id}",
            initial_comment="Git diff after prompt completion",
        )
    except GitError as e:
        logger.info(f"Skipping git diff upload for command {command_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to upload git diff for command {command_id}: {e}")


async def deliver_command_response(
    *,
    client: Any,
    channel_id: str,
    thread_ts: Optional[str],
    message_ts: str,
    prompt: str,
    output: str,
    command_id: int,
    duration_ms: Optional[int],
    cost_usd: Optional[float],
    is_error: bool,
    logger: Any,
    db: Any = None,
    detailed_output: Optional[str] = None,
    post_detail_button: bool = False,
    notify_on_snippet_failure: bool = False,
    api_with_retry: Optional[Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]] = None,
    terminal_style: bool = False,
    working_directory: Optional[str] = None,
    upload_git_diff: bool = False,
    git_tool_events: Optional[list[dict[str, Any]]] = None,
    upload_git_activity: bool = False,
) -> None:
    """Render and deliver final command output to Slack with shared formatting logic."""
    response_thread_ts = thread_ts

    async def _run_update(call: Callable[[], Awaitable[Any]]) -> Any:
        if api_with_retry:
            return await api_with_retry(call)
        return await call()

    if should_attach_file(output):
        blocks, file_content, _file_title = command_response_with_file(
            prompt=prompt,
            output=output,
            command_id=command_id,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            is_error=is_error,
            terminal_style=terminal_style,
        )
        blocks = _maybe_add_copy_output_button(blocks, command_id)
        await _run_update(
            lambda: client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=output[:100] + "..." if len(output) > 100 else output,
                blocks=blocks,
            )
        )
        try:
            detail_content = detailed_output or file_content
            DetailCache.store(command_id, detail_content)
            if db is not None:
                await db.store_command_detailed_output(command_id, detail_content)
            if post_detail_button:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=response_thread_ts,
                    text="📋 Detailed output available",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"📋 *Detailed output* " f"({len(detail_content):,} chars)"
                                ),
                            },
                            "accessory": {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "View Details",
                                    "emoji": True,
                                },
                                "action_id": "view_detailed_output",
                                "value": str(command_id),
                            },
                        },
                    ],
                )
        except Exception as post_error:
            logger.error(f"Failed to post detailed output button: {post_error}")
            if notify_on_snippet_failure:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=response_thread_ts,
                    text=f"⚠️ Could not post detailed output: {str(post_error)[:100]}",
                )
        if upload_git_activity and git_tool_events:
            await _maybe_upload_git_activity(
                client=client,
                channel_id=channel_id,
                thread_ts=response_thread_ts,
                command_id=command_id,
                git_tool_events=git_tool_events,
                logger=logger,
            )
        if upload_git_diff:
            await _maybe_upload_git_diff(
                client=client,
                channel_id=channel_id,
                thread_ts=response_thread_ts,
                command_id=command_id,
                working_directory=working_directory,
                logger=logger,
            )
        return

    message_blocks_list = command_response_with_tables(
        prompt=prompt,
        output=output,
        command_id=command_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        is_error=is_error,
        terminal_style=terminal_style,
    )
    if message_blocks_list:
        message_blocks_list[0] = _maybe_add_copy_output_button(message_blocks_list[0], command_id)
    await _run_update(
        lambda: client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=output[:100] + "..." if len(output) > 100 else output,
            blocks=message_blocks_list[0],
        )
    )
    for blocks in message_blocks_list[1:]:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=response_thread_ts,
            text="Table",
            blocks=blocks,
        )
    if upload_git_activity and git_tool_events:
        await _maybe_upload_git_activity(
            client=client,
            channel_id=channel_id,
            thread_ts=response_thread_ts,
            command_id=command_id,
            git_tool_events=git_tool_events,
            logger=logger,
        )
    if upload_git_diff:
        await _maybe_upload_git_diff(
            client=client,
            channel_id=channel_id,
            thread_ts=response_thread_ts,
            command_id=command_id,
            working_directory=working_directory,
            logger=logger,
        )
