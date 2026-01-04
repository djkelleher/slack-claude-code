"""Streaming message update utilities for Slack."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config import config
from src.utils.formatting import SlackFormatter


@dataclass
class StreamingMessageState:
    """Tracks state for a streaming Slack message update.

    Encapsulates accumulated output, throttling, and message reference.

    Parameters
    ----------
    channel_id : str
        The Slack channel ID.
    message_ts : str
        The timestamp of the message to update.
    prompt : str
        The original prompt being processed.
    client : Any
        The Slack WebClient for API calls.
    logger : Any
        Logger instance for this request.
    """

    channel_id: str
    message_ts: str
    prompt: str
    client: Any
    logger: Any
    accumulated_output: str = ""
    last_update_time: float = field(default=0.0)

    async def append_and_update(self, content: str) -> None:
        """Append content and update Slack message if throttle allows.

        Parameters
        ----------
        content : str
            New content to append to accumulated output.
        """
        # Limit accumulated output to prevent memory exhaustion
        if len(self.accumulated_output) < config.timeouts.streaming.max_accumulated_size:
            self.accumulated_output += content

        # Rate limit updates to avoid Slack API limits
        current_time = asyncio.get_running_loop().time()
        if current_time - self.last_update_time > config.timeouts.slack.message_update_throttle:
            self.last_update_time = current_time
            await self._send_update()

    async def _send_update(self) -> None:
        """Send throttled update to Slack."""
        try:
            text_preview = (
                self.accumulated_output[:100] + "..."
                if len(self.accumulated_output) > 100
                else self.accumulated_output
            )
            await self.client.chat_update(
                channel=self.channel_id,
                ts=self.message_ts,
                text=text_preview,
                blocks=SlackFormatter.streaming_update(self.prompt, self.accumulated_output),
            )
        except Exception as e:
            self.logger.warning(f"Failed to update message: {e}")

    async def finalize(self) -> None:
        """Send final update to mark streaming as complete."""
        try:
            text_preview = (
                self.accumulated_output[:100] + "..."
                if len(self.accumulated_output) > 100
                else self.accumulated_output
            )
            await self.client.chat_update(
                channel=self.channel_id,
                ts=self.message_ts,
                text=text_preview,
                blocks=SlackFormatter.streaming_update(
                    self.prompt, self.accumulated_output, is_complete=True
                ),
            )
        except Exception as e:
            self.logger.warning(f"Failed to finalize message: {e}")


def create_streaming_callback(state: StreamingMessageState) -> Callable:
    """Create a callback for executor.execute() that updates Slack messages.

    Parameters
    ----------
    state : StreamingMessageState
        The streaming state to update.

    Returns
    -------
    Callable
        Async callback function for on_chunk parameter.
    """

    async def on_chunk(msg) -> None:
        if msg.type == "assistant" and msg.content:
            await state.append_and_update(msg.content)

    return on_chunk
