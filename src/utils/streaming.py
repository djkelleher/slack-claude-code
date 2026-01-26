"""Streaming message update utilities for Slack."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from loguru import logger

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
    _heartbeat_task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)
    _is_idle: bool = field(default=False)
    db_session_id: Optional[int] = None

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

    def get_session_plan_path(self) -> str:
        """Get the expected session-specific plan file path.

        Returns
        -------
        str
            Full path to the session-specific plan file.
        """
        import os

        plans_dir = os.path.expanduser("~/.claude/plans")
        return os.path.join(plans_dir, self.get_session_plan_filename())

    def get_plan_file_path(self, working_directory: Optional[str] = None) -> Optional[str]:
        """Get the plan file path if one was written during plan mode.

        Checks in order:
        1. Session-specific plan file (plan-session-{id}.md) - highest priority
        2. Write tool activities for direct writes to ~/.claude/plans/
        3. Task (Plan subagent) results for plan file paths mentioned in output
        4. Fallback: scan ~/.claude/plans/ for recently modified .md files

        Parameters
        ----------
        working_directory : str, optional
            Unused, kept for API compatibility.

        Returns
        -------
        str or None
            Path to the plan file, or None if not found.
        """
        import os
        import re

        plans_dir = os.path.expanduser("~/.claude/plans")

        # Log available tool activities for debugging
        tool_names = [f"{t.name}({t.id[:8]})" for t in self.tool_activities.values()]
        logger.debug(f"Looking for plan file. Available tools: {tool_names}")

        # HIGHEST PRIORITY: Check for session-specific plan file
        # This prevents race conditions when multiple sessions run in parallel
        if self.db_session_id:
            session_plan_path = self.get_session_plan_path()
            if os.path.isfile(session_plan_path):
                logger.info(
                    f"Plan file found via session-specific path: {session_plan_path}"
                )
                return session_plan_path
            logger.debug(
                f"Session-specific plan file not found at {session_plan_path}, "
                "checking other sources"
            )

        # Check tracked Write tool activities for plan files
        plan_write_candidates = []
        for tool in self.tool_activities.values():
            if tool.name == "Write" and not tool.is_error:
                file_path = tool.input.get("file_path", "")
                # Expand ~ in the file path before comparing
                expanded_file_path = os.path.expanduser(file_path)
                if expanded_file_path.endswith(".md"):
                    # Prioritize files in ~/.claude/plans/
                    if expanded_file_path.startswith(plans_dir):
                        logger.info(f"Plan file found via Write tool activity: {expanded_file_path}")
                        return expanded_file_path
                    # Track other .md files as potential plan files
                    elif "plan" in expanded_file_path.lower():
                        plan_write_candidates.append(expanded_file_path)
                        logger.debug(f"Potential plan file via Write activity: {expanded_file_path}")

        # If we found a Write to a plan-named file outside ~/.claude/plans/, use that
        if plan_write_candidates:
            # Prefer most recently tracked
            result = plan_write_candidates[-1]
            if os.path.isfile(result):
                logger.info(f"Plan file found via Write tool activity (fallback): {result}")
                return result

        # Check Task (Plan subagent) results for plan file paths
        # The subagent result text often contains the file path it wrote to
        for tool in self.tool_activities.values():
            if tool.name == "Task" and tool.full_result and not tool.is_error:
                logger.debug(f"Checking Task tool result for plan file: {tool.full_result[:500]}...")
                # Look for paths like ~/.claude/plans/something.md or /home/user/.claude/plans/something.md
                # Use broader pattern to catch various filename formats (words, hyphens, underscores, dots)
                patterns = [
                    r"~/.claude/plans/[^\s\"']+\.md",
                    rf"{re.escape(plans_dir)}/[^\s\"']+\.md",
                    r"/home/[^/]+/.claude/plans/[^\s\"']+\.md",
                ]
                for pattern in patterns:
                    match = re.search(pattern, tool.full_result)
                    if match:
                        path = match.group(0)
                        # Expand ~ if present
                        expanded = os.path.expanduser(path)
                        if os.path.isfile(expanded):
                            logger.info(f"Plan file found via Task subagent result: {expanded}")
                            return expanded
                        else:
                            logger.debug(f"Path from Task result not a file: {expanded}")

        # Fallback: scan directory for recently modified plan files
        # Note: This fallback can cause race conditions with parallel sessions
        # Session-specific naming (above) should be preferred
        import time

        candidates = []
        now = time.time()
        max_age_seconds = config.timeouts.limits.plan_file_max_age_seconds

        # Scan ~/.claude/plans/ directory
        if os.path.isdir(plans_dir):
            try:
                for entry in os.scandir(plans_dir):
                    if entry.is_file() and entry.name.endswith(".md"):
                        mtime = entry.stat().st_mtime
                        if now - mtime < max_age_seconds:
                            candidates.append((entry.path, mtime))
                            logger.debug(f"Found candidate plan file: {entry.path} (age: {now - mtime:.0f}s)")
            except OSError as e:
                logger.warning(f"Failed to scan plans directory {plans_dir}: {e}")
        else:
            logger.debug(f"Plans directory does not exist: {plans_dir}")

        # Also check working directory for plan files (in case Claude wrote there)
        if working_directory:
            try:
                wd = os.path.expanduser(working_directory)
                if os.path.isdir(wd):
                    for entry in os.scandir(wd):
                        if entry.is_file() and entry.name.lower() == "plan.md":
                            mtime = entry.stat().st_mtime
                            if now - mtime < max_age_seconds:
                                candidates.append((entry.path, mtime))
                                logger.debug(f"Found candidate plan file in working dir: {entry.path}")
            except OSError as e:
                logger.debug(f"Failed to scan working directory {working_directory}: {e}")

        if not candidates:
            logger.debug(f"No recent plan files found in {plans_dir} or working directory")
            return None

        # Return the most recently modified plan file
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = candidates[0][0]
        logger.info(f"Plan file found via directory scan fallback: {result}")
        return result

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
