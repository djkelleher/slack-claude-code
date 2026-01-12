"""PTY session management for persistent Claude Code sessions.

Uses pexpect to maintain a long-running Claude Code process with PTY interaction.
"""

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

import pexpect

from ..hooks.registry import HookRegistry, create_context
from ..hooks.types import HookEvent, HookEventType
from .parser import TerminalOutputParser
from .process import ClaudeProcess
from .types import (
    PTYSessionConfig,
    ResponseChunk,
    SessionResponse,
    SessionState,
)

logger = logging.getLogger(__name__)


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
        self.process = ClaudeProcess(config)
        self.parser = TerminalOutputParser()

        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.accumulated_output = ""

        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        """Start the Claude Code process with PTY."""
        try:
            await self.process.spawn()

            prompt_found = await self.process.wait_for_prompt()
            if not prompt_found:
                await self._set_state(SessionState.ERROR)
                return False

            await self.process.flush_startup_output()
            await self._set_state(SessionState.IDLE)

            await HookRegistry.emit(
                HookEvent(
                    event_type=HookEventType.SESSION_START,
                    context=create_context(
                        session_id=self.session_id,
                        working_directory=self.config.working_directory,
                    ),
                    data={"pid": self.pid},
                )
            )

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

        Parameters
        ----------
        prompt : str
            The prompt to send to Claude.
        on_chunk : Callable, optional
            Callback for streaming chunks.
        timeout : float
            Maximum time to wait for response.

        Returns
        -------
        SessionResponse
            Complete output from Claude.
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
                await asyncio.sleep(0.5)

                logger.info(f"Sending prompt to Claude: {prompt[:50]}...")
                self.process.send(prompt + "\r")

                await asyncio.sleep(0.2)

                try:
                    immediate = self.process.read_nonblocking(size=1024, timeout=0.5)
                    if immediate:
                        logger.info(f"Immediate output after prompt ({len(immediate)} chars)")
                        self.accumulated_output = immediate
                except pexpect.TIMEOUT:
                    logger.info("No immediate output after prompt")

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

        Parameters
        ----------
        approved : bool
            True to approve, False to deny.

        Returns
        -------
        bool
            True if response was sent successfully.
        """
        if self.state != SessionState.AWAITING_APPROVAL:
            return False

        response = "y" if approved else "n"
        self.process.sendline(response)
        self.last_activity = datetime.now()
        return True

    async def interrupt(self) -> bool:
        """Send Ctrl+C to interrupt the current operation."""
        if self.process.is_alive():
            self.process.sendcontrol("c")
            self.last_activity = datetime.now()
            return True
        return False

    async def stop(self) -> None:
        """Stop the session gracefully."""
        await self._set_state(SessionState.STOPPING)
        await self.process.terminate()
        await self._set_state(SessionState.STOPPED)

        await HookRegistry.emit(
            HookEvent(
                event_type=HookEventType.SESSION_END,
                context=create_context(
                    session_id=self.session_id,
                    working_directory=self.config.working_directory,
                ),
                data={"duration_seconds": (datetime.now() - self.created_at).total_seconds()},
            )
        )

    def is_alive(self) -> bool:
        """Check if the session process is still running."""
        return self.process.is_alive()

    @property
    def pid(self) -> Optional[int]:
        """Get the process ID of the Claude process."""
        return self.process.pid

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

            if current_time - start_time > timeout:
                raise pexpect.TIMEOUT("Overall timeout exceeded")

            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: self.process.read_nonblocking(size=4096, timeout=0.05),
                    ),
                    timeout=self.config.read_timeout,
                )
            except asyncio.TimeoutError:
                if (
                    current_time - last_output_time > self.config.inactivity_timeout
                    and self.accumulated_output
                ):
                    parsed = self.parser.parse("")
                    if parsed.has_prompt:
                        break
                continue

            if data:
                last_output_time = current_time
                self.accumulated_output += data

                logger.debug(
                    f"PTY output: {data[:100]}..." if len(data) > 100 else f"PTY output: {data}"
                )

                if self.on_output:
                    await self.on_output(data)

                parsed = self.parser.parse_incremental(data)

                if parsed.has_permission_request:
                    permission_detected = True
                    await self._set_state(SessionState.AWAITING_APPROVAL)

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

                if parsed.has_prompt:
                    break

            await asyncio.sleep(0.01)

        clean_output = self.parser.clean_for_slack(self.accumulated_output)

        return SessionResponse(
            output=clean_output.strip(),
            success=True,
            was_permission_request=permission_detected,
        )

    async def _set_state(self, new_state: SessionState) -> None:
        """Update session state and notify callback."""
        self.state = new_state
        if self.on_state_change:
            await self.on_state_change(new_state)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal."""
        self.process.resize(rows, cols)
