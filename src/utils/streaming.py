"""Streaming message update utilities for Slack."""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.config import PLANS_DIR, config
from src.utils.formatters.streaming import streaming_update

if TYPE_CHECKING:
    from src.claude.streaming import ToolActivity

# Number of consecutive failures before triggering error callback
MAX_CONSECUTIVE_UPDATE_FAILURES = 3


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
    _heartbeat_task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)
    _is_idle: bool = field(default=False)
    db_session_id: Optional[int] = None
    on_error: Optional[Callable[[str], Awaitable[None]]] = None
    _consecutive_failures: int = field(default=0)
    _error_callback_triggered: bool = field(default=False)
    started_at: float = field(default_factory=time.time)

    def get_tool_list(self) -> list["ToolActivity"]:
        """Get list of tracked tool activities."""
        return list(self.tool_activities.values())

    def get_session_plan_filename(self) -> str:
        """Generate session-specific plan filename.

        Returns
        -------
        str
            Filename like 'plan-session-123.md' for the current session.
        """
        if self.db_session_id:
            return f"plan-session-{self.db_session_id}.md"
        return "plan.md"

    def get_execution_plan_filename(self, execution_id: Optional[str] = None) -> str:
        """Generate execution-specific plan filename.

        Parameters
        ----------
        execution_id : str, optional
            Unique execution identifier to disambiguate plan files.

        Returns
        -------
        str
            Filename like 'plan-session-123-<execution_id>.md' when available.
        """
        base = f"plan-session-{self.db_session_id}" if self.db_session_id else "plan"
        if execution_id:
            return f"{base}-{execution_id}.md"
        return f"{base}.md"

    def get_session_plan_path(self) -> str:
        """Get the expected session-specific plan file path.

        Returns
        -------
        str
            Full path to the session-specific plan file.
        """
        plans_dir = PLANS_DIR
        return os.path.join(plans_dir, self.get_session_plan_filename())

    def get_execution_plan_path(self, execution_id: Optional[str] = None) -> str:
        """Get the execution-specific plan file path.

        Parameters
        ----------
        execution_id : str, optional
            Unique execution identifier to disambiguate plan files.

        Returns
        -------
        str
            Full path to the execution-specific plan file.
        """
        plans_dir = PLANS_DIR
        return os.path.join(plans_dir, self.get_execution_plan_filename(execution_id))

    def get_recent_plan_write_path(self, min_mtime: float) -> Optional[str]:
        """Get most recent plan file written during this session.

        Parameters
        ----------
        min_mtime : float
            Minimum file modification time (epoch seconds) to consider.

        Returns
        -------
        str or None
            Path to the most recent plan file written, or None if not found.
        """
        plans_dir = PLANS_DIR
        in_plans_dir: list[tuple[str, float]] = []
        elsewhere: list[tuple[str, float]] = []

        for tool in self.tool_activities.values():
            if tool.name not in ("Write", "Edit") or tool.is_error:
                continue
            file_path = tool.input.get("file_path", "")
            if not file_path:
                continue
            expanded = os.path.expanduser(file_path)
            if not expanded.endswith(".md"):
                continue
            if os.path.isfile(expanded):
                mtime = os.path.getmtime(expanded)
                if mtime >= min_mtime:
                    if expanded.startswith(plans_dir):
                        in_plans_dir.append((expanded, mtime))
                    else:
                        elsewhere.append((expanded, mtime))

        if not in_plans_dir and not elsewhere:
            return None
        if in_plans_dir:
            in_plans_dir.sort(key=lambda x: x[1], reverse=True)
            return in_plans_dir[0][0]
        elsewhere.sort(key=lambda x: x[1], reverse=True)
        return elsewhere[0][0]

    def get_recent_plan_file_path(
        self,
        min_mtime: float,
        max_mtime: Optional[float] = None,
    ) -> Optional[str]:
        """Find a plan file in ~/.claude/plans modified within a time window.

        Parameters
        ----------
        min_mtime : float
            Minimum file modification time (epoch seconds) to consider.
        max_mtime : float, optional
            Maximum file modification time (epoch seconds) to consider.

        Returns
        -------
        str or None
            Path to the most recent plan file in the window, or None if not found.
        """
        plans_dir = PLANS_DIR
        if not os.path.isdir(plans_dir):
            return None

        candidates: list[tuple[str, float]] = []
        try:
            for entry in os.scandir(plans_dir):
                if not entry.is_file() or not entry.name.endswith(".md"):
                    continue
                mtime = entry.stat().st_mtime
                if mtime < min_mtime:
                    continue
                if max_mtime is not None and mtime > max_mtime:
                    continue
                candidates.append((entry.path, mtime))
        except OSError as e:
            logger.debug(f"Failed to scan plans directory {plans_dir}: {e}")
            return None

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def start_heartbeat(self) -> None:
        """Start the heartbeat task to show progress during idle periods."""
        if self._heartbeat_task is None:
            loop = asyncio.get_running_loop()
            self.last_activity_time = loop.time()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        """Stop the heartbeat task and await its cancellation."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
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
            if content and self.accumulated_output:
                # Ensure proper spacing between chunks
                last_char = self.accumulated_output[-1]
                first_char = content[0] if content else ""

                # Add space if previous chunk ends with sentence punctuation
                # and next chunk starts with a letter (likely new sentence)
                if last_char in ".!?:)" and first_char.isalpha():
                    self.accumulated_output += " "
                # Add space if chunks would merge words (letter followed by letter)
                elif last_char.isalnum() and first_char.isalnum():
                    self.accumulated_output += " "

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
                blocks=streaming_update(
                    self.prompt,
                    output,
                    tool_activities=tool_list,
                ),
            )
            # Reset failure counter on success
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            self.logger.warning(
                f"Failed to update message (attempt {self._consecutive_failures}): {e}"
            )
            # Trigger error callback after repeated failures (only once)
            if (
                self._consecutive_failures >= MAX_CONSECUTIVE_UPDATE_FAILURES
                and self.on_error
                and not self._error_callback_triggered
            ):
                self._error_callback_triggered = True
                try:
                    await self.on_error(
                        f"Failed to update Slack message after {self._consecutive_failures} "
                        f"consecutive attempts. Last error: {e}"
                    )
                except Exception as callback_error:
                    self.logger.error(f"Error callback failed: {callback_error}")

    async def finalize(self) -> None:
        """Send final update to mark streaming as complete."""
        # Stop heartbeat task
        await self.stop_heartbeat()

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
                blocks=streaming_update(
                    self.prompt,
                    self.accumulated_output,
                    tool_activities=tool_list,
                    is_complete=True,
                ),
            )
        except Exception as e:
            self.logger.warning(f"Failed to finalize message: {e}")
            # Try to notify user of finalization failure (if callback available and not already triggered)
            if self.on_error and not self._error_callback_triggered:
                self._error_callback_triggered = True
                try:
                    await self.on_error(
                        f"Failed to finalize Slack message: {e}. "
                        "The response may not be fully visible."
                    )
                except Exception as callback_error:
                    self.logger.error(f"Error callback failed: {callback_error}")


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
