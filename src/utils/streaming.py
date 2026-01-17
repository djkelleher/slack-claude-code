"""Streaming message update utilities for Slack."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from src.config import config
from src.utils.formatting import SlackFormatter

if TYPE_CHECKING:
    from src.claude.streaming import ToolActivity


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
    smart_concat : bool
        If True, add newlines between chunks for better readability.
    track_tools : bool
        If True, track tool activities for display.
    """

    channel_id: str
    message_ts: str
    prompt: str
    client: Any
    logger: Any
    smart_concat: bool = False
    track_tools: bool = False
    accumulated_output: str = ""
    last_update_time: float = field(default=0.0)
    last_activity_time: float = field(default=0.0)
    tool_activities: dict[str, "ToolActivity"] = field(default_factory=dict)
    _last_chunk_was_newline: bool = field(default=False)
    _heartbeat_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _is_idle: bool = field(default=False)

    def get_tool_list(self) -> list["ToolActivity"]:
        """Get list of tracked tool activities."""
        return list(self.tool_activities.values())

    def start_heartbeat(self) -> None:
        """Start the heartbeat task to show progress during idle periods."""
        if self._heartbeat_task is None:
            loop = asyncio.get_running_loop()
            self.last_activity_time = loop.time()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """Background task that updates message during idle periods."""
        try:
            while True:
                await asyncio.sleep(config.timeouts.slack.heartbeat_interval)

                loop = asyncio.get_running_loop()
                current_time = loop.time()
                idle_time = current_time - self.last_activity_time

                # If we've been idle for a while, show "still working" indicator
                if idle_time >= config.timeouts.slack.heartbeat_threshold:
                    if not self._is_idle:
                        self._is_idle = True
                        await self._send_update(show_idle=True)
                        self.logger.debug(
                            f"Showing idle indicator after {idle_time:.1f}s of inactivity"
                        )
        except asyncio.CancelledError:
            pass

    async def append_and_update(
        self,
        content: str,
        tools: list["ToolActivity"] = None,
    ) -> None:
        """Append content and update Slack message if throttle allows.

        Parameters
        ----------
        content : str
            New content to append to accumulated output.
        tools : list[ToolActivity], optional
            Tool activities to track.
        """
        # Record activity time and reset idle state
        loop = asyncio.get_running_loop()
        current_time = loop.time()
        if content or tools:
            self.last_activity_time = current_time
            if self._is_idle:
                self._is_idle = False

        # Track tool activities
        if self.track_tools and tools:
            for tool in tools:
                if tool.id in self.tool_activities:
                    existing = self.tool_activities[tool.id]
                    if tool.result is not None:
                        existing.result = tool.result
                        existing.full_result = tool.full_result
                        existing.is_error = tool.is_error
                        existing.duration_ms = tool.duration_ms
                else:
                    self.tool_activities[tool.id] = tool

        # Limit accumulated output to prevent memory exhaustion
        if len(self.accumulated_output) < config.timeouts.streaming.max_accumulated_size:
            if self.smart_concat and content:
                # Smart chunk concatenation: add newlines between chunks for readability
                if self.accumulated_output and not self._last_chunk_was_newline:
                    if len(content) >= 10 or content.strip():
                        if not self.accumulated_output.endswith(("\n", " ", "\t")):
                            self.accumulated_output += "\n\n"
                self._last_chunk_was_newline = content.endswith("\n") if content else False
            self.accumulated_output += content

        # Rate limit updates to avoid Slack API limits
        if current_time - self.last_update_time > config.timeouts.slack.message_update_throttle:
            self.last_update_time = current_time
            await self._send_update()

    async def _send_update(self, show_idle: bool = False) -> None:
        """Send throttled update to Slack.

        Parameters
        ----------
        show_idle : bool
            If True, append an idle indicator to show we're still working.
        """
        try:
            text_preview = (
                self.accumulated_output[:100] + "..."
                if len(self.accumulated_output) > 100
                else self.accumulated_output
            )
            tool_list = self.get_tool_list() if self.track_tools else None

            # Add idle indicator to output if needed
            output = self.accumulated_output
            if show_idle:
                output += "\n\n_:hourglass_flowing_sand: Still working..._"

            await self.client.chat_update(
                channel=self.channel_id,
                ts=self.message_ts,
                text=text_preview,
                blocks=SlackFormatter.streaming_update(
                    self.prompt,
                    output,
                    tool_activities=tool_list,
                ),
            )
        except Exception as e:
            self.logger.warning(f"Failed to update message: {e}")

    async def finalize(self) -> None:
        """Send final update to mark streaming as complete."""
        # Stop heartbeat task
        self.stop_heartbeat()

        try:
            text_preview = (
                self.accumulated_output[:100] + "..."
                if len(self.accumulated_output) > 100
                else self.accumulated_output
            )
            tool_list = self.get_tool_list() if self.track_tools else None
            await self.client.chat_update(
                channel=self.channel_id,
                ts=self.message_ts,
                text=text_preview,
                blocks=SlackFormatter.streaming_update(
                    self.prompt,
                    self.accumulated_output,
                    tool_activities=tool_list,
                    is_complete=True,
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
        content = msg.content if msg.type == "assistant" else ""
        tools = msg.tool_activities if state.track_tools else None
        if content or tools:
            await state.append_and_update(content or "", tools)

    return on_chunk
