"""PTY session management for persistent Claude Code sessions.

Uses pexpect to maintain a long-running Claude Code process with PTY interaction.
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Awaitable, Optional

import pexpect
import pexpect.popen_spawn

from ..config import config
from ..hooks import HookRegistry, HookEvent, HookEventType, create_context
from .parser import TerminalOutputParser, ParsedOutput, OutputType


class SessionState(Enum):
    """State of a PTY session."""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    AWAITING_APPROVAL = "awaiting_approval"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class PTYSessionConfig:
    """Configuration for a PTY session.

    Defaults are pulled from centralized config.timeouts.pty.
    """

    working_directory: str = "~"
    inactivity_timeout: float = field(
        default_factory=lambda: config.timeouts.pty.inactivity
    )
    read_timeout: float = field(default_factory=lambda: config.timeouts.pty.read)
    startup_timeout: float = field(default_factory=lambda: config.timeouts.pty.startup)
    cols: int = 120
    rows: int = 40
    claude_args: list[str] = field(default_factory=list)


@dataclass
class ResponseChunk:
    """A chunk of response from Claude."""

    content: str
    output_type: OutputType = OutputType.TEXT
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    is_final: bool = False
    is_permission_request: bool = False
    raw: str = ""


@dataclass
class SessionResponse:
    """Complete response from a session prompt."""

    output: str
    success: bool = True
    error: Optional[str] = None
    was_permission_request: bool = False


class PTYSession:
    """Manages a persistent Claude Code PTY session.

    Keeps Claude Code running in interactive mode and allows sending
    multiple prompts without restarting the process.
    """

    def __init__(
        self,
        session_id: str,
        config: PTYSessionConfig,
        on_state_change: Optional[Callable[[SessionState], Awaitable[None]]] = None,
        on_output: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.on_state_change = on_state_change
        self.on_output = on_output

        self.state = SessionState.STARTING
        self.child: Optional[pexpect.spawn] = None
        self.parser = TerminalOutputParser()

        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.accumulated_output = ""

        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        """Start the Claude Code process with PTY."""
        try:
            cwd = Path(self.config.working_directory).expanduser()
            if not cwd.exists():
                cwd = Path.home()

            # Build command
            cmd = "claude"
            args = self.config.claude_args.copy() if self.config.claude_args else []

            # Set environment
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["FORCE_COLOR"] = "1"
            env["COLUMNS"] = str(self.config.cols)
            env["LINES"] = str(self.config.rows)

            # Spawn the process with PTY
            self.child = pexpect.spawn(
                cmd,
                args=args,
                cwd=str(cwd),
                env=env,
                encoding="utf-8",
                timeout=self.config.startup_timeout,
                dimensions=(self.config.rows, self.config.cols),
            )

            # Wait for initial prompt
            await self._wait_for_prompt()
            await self._set_state(SessionState.IDLE)

            # Emit SESSION_START hook
            await HookRegistry.emit(HookEvent(
                event_type=HookEventType.SESSION_START,
                context=create_context(
                    session_id=self.session_id,
                    working_directory=str(cwd),
                ),
                data={"pid": self.pid},
            ))

            return True

        except pexpect.TIMEOUT:
            await self._set_state(SessionState.ERROR)
            return False
        except pexpect.EOF:
            await self._set_state(SessionState.ERROR)
            return False
        except Exception:
            await self._set_state(SessionState.ERROR)
            raise

    async def send_prompt(
        self,
        prompt: str,
        on_chunk: Optional[Callable[[ResponseChunk], Awaitable[None]]] = None,
        timeout: float = 300.0,
    ) -> SessionResponse:
        """Send a prompt and collect the response.

        Args:
            prompt: The prompt to send to Claude
            on_chunk: Optional callback for streaming chunks
            timeout: Maximum time to wait for response

        Returns:
            SessionResponse with the complete output
        """
        if self.state not in (SessionState.IDLE, SessionState.AWAITING_APPROVAL):
            return SessionResponse(
                output="",
                success=False,
                error=f"Session not ready: {self.state.value}",
            )

        async with self._lock:
            await self._set_state(SessionState.BUSY)
            self.accumulated_output = ""
            self.parser.reset()
            self.last_activity = datetime.now()

            try:
                # Send the prompt
                self.child.sendline(prompt)

                # Collect output until prompt returns or timeout
                response = await self._read_until_prompt(
                    on_chunk=on_chunk,
                    timeout=timeout,
                )

                await self._set_state(SessionState.IDLE)
                return response

            except pexpect.TIMEOUT:
                await self._set_state(SessionState.ERROR)
                return SessionResponse(
                    output=self.accumulated_output,
                    success=False,
                    error=f"Command timed out after {timeout} seconds",
                )
            except pexpect.EOF:
                await self._set_state(SessionState.STOPPED)
                return SessionResponse(
                    output=self.accumulated_output,
                    success=False,
                    error="Claude process terminated unexpectedly",
                )
            except Exception as e:
                await self._set_state(SessionState.ERROR)
                return SessionResponse(
                    output=self.accumulated_output,
                    success=False,
                    error=str(e),
                )

    async def respond_to_approval(self, approved: bool) -> bool:
        """Send approval response (y/n) to a pending permission request.

        Args:
            approved: True to approve, False to deny

        Returns:
            True if response was sent successfully
        """
        if self.state != SessionState.AWAITING_APPROVAL:
            return False

        response = "y" if approved else "n"
        self.child.sendline(response)
        self.last_activity = datetime.now()
        return True

    async def interrupt(self) -> bool:
        """Send Ctrl+C to interrupt the current operation."""
        if self.child and self.child.isalive():
            self.child.sendcontrol("c")
            self.last_activity = datetime.now()
            return True
        return False

    async def stop(self) -> None:
        """Stop the session gracefully."""
        await self._set_state(SessionState.STOPPING)

        grace_period = config.timeouts.pty.stop_grace

        if self.child and self.child.isalive():
            # Try graceful exit first
            self.child.sendline("/exit")
            await asyncio.sleep(grace_period)

            if self.child.isalive():
                self.child.sendcontrol("c")
                await asyncio.sleep(grace_period)

            if self.child.isalive():
                self.child.terminate(force=True)

        await self._set_state(SessionState.STOPPED)

        # Emit SESSION_END hook
        await HookRegistry.emit(HookEvent(
            event_type=HookEventType.SESSION_END,
            context=create_context(
                session_id=self.session_id,
                working_directory=self.config.working_directory,
            ),
            data={"duration_seconds": (datetime.now() - self.created_at).total_seconds()},
        ))

    def is_alive(self) -> bool:
        """Check if the session process is still running."""
        return self.child is not None and self.child.isalive()

    @property
    def pid(self) -> Optional[int]:
        """Get the process ID of the Claude process."""
        if self.child is not None:
            return self.child.pid
        return None

    async def _wait_for_prompt(self) -> None:
        """Wait for the initial Claude prompt to appear."""
        loop = asyncio.get_event_loop()

        def read_until_prompt():
            # Look for common prompt patterns
            patterns = [
                r">\s*$",
                r"\?\s*$",
                r"claude\s*>\s*$",
            ]

            for pattern in patterns:
                try:
                    self.child.expect(pattern, timeout=self.config.startup_timeout)
                    return True
                except pexpect.TIMEOUT:
                    continue
                except pexpect.EOF:
                    return False

            return False

        await loop.run_in_executor(None, read_until_prompt)

    async def _read_until_prompt(
        self,
        on_chunk: Optional[Callable[[ResponseChunk], Awaitable[None]]] = None,
        timeout: float = 300.0,
    ) -> SessionResponse:
        """Read output until a prompt is detected.

        Uses non-blocking reads with asyncio to allow for streaming callbacks.
        """
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_output_time = start_time
        permission_detected = False

        while True:
            current_time = loop.time()

            # Check overall timeout
            if current_time - start_time > timeout:
                raise pexpect.TIMEOUT("Overall timeout exceeded")

            # Non-blocking read
            try:
                # Read available data with short timeout
                data = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: self._read_nonblock(),
                    ),
                    timeout=self.config.read_timeout,
                )
            except asyncio.TimeoutError:
                # Check inactivity timeout
                if (
                    current_time - last_output_time > self.config.inactivity_timeout
                    and self.accumulated_output
                ):
                    # No output for a while and we have some output - might be done
                    # Do one more check for prompt
                    parsed = self.parser.parse("")
                    if parsed.has_prompt:
                        break
                continue

            if data:
                last_output_time = current_time
                self.accumulated_output += data

                if self.on_output:
                    await self.on_output(data)

                # Parse the output
                parsed = self.parser.parse_incremental(data)

                # Check for permission request
                if parsed.has_permission_request:
                    permission_detected = True
                    await self._set_state(SessionState.AWAITING_APPROVAL)

                # Send chunk callback
                if on_chunk and parsed.chunks:
                    for chunk_data in parsed.chunks:
                        chunk = ResponseChunk(
                            content=chunk_data.text,
                            output_type=chunk_data.output_type,
                            tool_name=chunk_data.tool_name,
                            tool_input=chunk_data.tool_input,
                            is_final=chunk_data.is_prompt,
                            is_permission_request=chunk_data.is_permission_request,
                            raw=chunk_data.raw,
                        )
                        await on_chunk(chunk)

                # Check if we got a prompt (response complete)
                if parsed.has_prompt:
                    break

            # Small delay to prevent busy loop
            await asyncio.sleep(0.01)

        # Clean up the accumulated output (strip ANSI, etc.)
        clean_output = self.parser.strip_ansi(self.accumulated_output)

        return SessionResponse(
            output=clean_output.strip(),
            success=True,
            was_permission_request=permission_detected,
        )

    def _read_nonblock(self) -> str:
        """Read available data from the PTY without blocking.

        Returns empty string if no data available.
        """
        try:
            self.child.read_nonblocking(size=4096, timeout=0.05)
            return self.child.before or ""
        except pexpect.TIMEOUT:
            return ""
        except pexpect.EOF:
            raise

    async def _set_state(self, new_state: SessionState) -> None:
        """Update session state and notify callback."""
        self.state = new_state
        if self.on_state_change:
            await self.on_state_change(new_state)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal."""
        if self.child:
            self.child.setwinsize(rows, cols)
            self.config.rows = rows
            self.config.cols = cols
