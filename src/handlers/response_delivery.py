"""Shared Slack response delivery helpers for command execution paths."""

from typing import Any, Awaitable, Callable, Optional

from src.utils.detail_cache import DetailCache
from src.utils.formatters.command import (
    command_response_with_file,
    command_response_with_tables,
    should_attach_file,
)


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
    detailed_output: Optional[str] = None,
    post_detail_button: bool = False,
    notify_on_snippet_failure: bool = False,
    api_with_retry: Optional[Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]] = None,
    terminal_style: bool = False,
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
                                    f"📋 *Detailed output* "
                                    f"({len(detail_content):,} chars)"
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
