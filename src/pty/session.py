"""PTY session management for persistent Codex CLI sessions.

Uses pexpect to maintain a long-running Codex CLI process with PTY interaction.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

import pexpect
from loguru import logger

from src.codex.streaming import StreamMessage, StreamParser
from src.codex.subprocess_executor import ExecutionResult

from .process import CodexProcess
from .types import PTYSessionConfig, SessionState


@dataclass
class SessionResponse:
    """Complete response from a session prompt."""

    output: str
    detailed_output: str = ""
    session_id: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None


class PTYSession:
    """Manages a persistent Codex CLI PTY session.

    Keeps Codex CLI running in interactive mode and allows sending
    multiple prompts without restarting the process.
    """

    def __init__(
        self,
        session_id: str,
        config: PTYSessionConfig,
        on_state_change: Optional[Callable[[SessionState], Awaitable[None]]] = None,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.on_state_change = on_state_change

        self.state = SessionState.STARTING
        self.process = CodexProcess(config)
        self.parser = StreamParser()

        self.codex_session_id: Optional[str] = None
        self.created_at = datetime.now()
        self.last_activity = datetime.now()

        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        """Start the Codex CLI process with PTY."""
        try:
            await self.process.spawn()

            ready = await self.process.wait_for_ready()
            if not ready:
                await self._set_state(SessionState.ERROR)
                return False

            # Flush and parse startup output to get session ID
            startup_output = await self.process.flush_startup_output()
            if startup_output:
                for line in startup_output.split("\n"):
                    msg = self.parser.parse_line(line)
                    if msg and msg.session_id:
                        self.codex_session_id = msg.session_id
                        logger.info(f"Got Codex session ID: {self.codex_session_id}")

            await self._set_state(SessionState.IDLE)
            logger.info(f"PTY session {self.session_id} started (PID: {self.pid})")

            return True

        except pexpect.TIMEOUT:
            logger.error(f"Timeout starting PTY session {self.session_id}")
            await self._set_state(SessionState.ERROR)
            return False
        except pexpect.EOF:
            logger.error(f"EOF starting PTY session {self.session_id}")
            await self._set_state(SessionState.ERROR)
            return False
        except Exception as e:
            logger.error(f"Error starting PTY session {self.session_id}: {e}")
            await self._set_state(SessionState.ERROR)
            raise

    async def send_prompt(
        self,
        prompt: str,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        timeout: float = 216000.0,  # 60 hours
    ) -> ExecutionResult:
        """Send a prompt and collect the response.

        Parameters
        ----------
        prompt : str
            The prompt to send to Codex.
        on_chunk : Callable, optional
            Callback for streaming chunks.
        timeout : float
            Maximum time to wait for response.

        Returns
        -------
        ExecutionResult
            Complete output from Codex.
        """
        if self.state != SessionState.IDLE:
            return ExecutionResult(
                success=False,
                output="",
                error=f"Session not ready: {self.state.value}",
            )

        async with self._lock:
            await self._set_state(SessionState.BUSY)
            self.parser.reset()
            self.last_activity = datetime.now()

            accumulated_output = ""
            accumulated_detailed = ""
            result_session_id = self.codex_session_id
            cost_usd = None
            duration_ms = None
            error_msg = None

            try:
                # Small delay before sending
                await asyncio.sleep(0.2)

                # Send the prompt
                logger.info(f"Sending prompt to Codex: {prompt[:50]}...")
                self.process.sendline(prompt)

                # Read response
                response = await self._read_response(
                    on_chunk=on_chunk,
                    timeout=timeout,
                )

                accumulated_output = response.output
                accumulated_detailed = response.detailed_output
                if response.session_id:
                    result_session_id = response.session_id
                    self.codex_session_id = response.session_id
                cost_usd = response.cost_usd
                duration_ms = response.duration_ms
                error_msg = response.error

                await self._set_state(SessionState.IDLE)

                return ExecutionResult(
                    success=response.success and not error_msg,
                    output=accumulated_output,
                    detailed_output=accumulated_detailed,
                    session_id=result_session_id,
                    error=error_msg,
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                )

            except pexpect.TIMEOUT:
                await self._set_state(SessionState.ERROR)
                return ExecutionResult(
                    success=False,
                    output=accumulated_output,
                    detailed_output=accumulated_detailed,
                    session_id=result_session_id,
                    error=f"Command timed out after {timeout} seconds",
                )
            except pexpect.EOF:
                await self._set_state(SessionState.STOPPED)
                return ExecutionResult(
                    success=False,
                    output=accumulated_output,
                    detailed_output=accumulated_detailed,
                    session_id=result_session_id,
                    error="Codex process terminated unexpectedly",
                )
            except Exception as e:
                logger.error(f"Error in send_prompt: {e}")
                await self._set_state(SessionState.ERROR)
                return ExecutionResult(
                    success=False,
                    output=accumulated_output,
                    detailed_output=accumulated_detailed,
                    session_id=result_session_id,
                    error=str(e),
                )

    async def _read_response(
        self,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        timeout: float = 216000.0,
    ) -> SessionResponse:
        """Read output until response is complete.

        Uses non-blocking reads with asyncio to allow for streaming callbacks.
        """
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_output_time = start_time

        accumulated_output = ""
        accumulated_detailed = ""
        session_id = self.codex_session_id
        cost_usd = None
        duration_ms = None
        error_msg = None
        is_complete = False

        while not is_complete:
            current_time = loop.time()

            # Check overall timeout
            if current_time - start_time > timeout:
                raise pexpect.TIMEOUT("Overall timeout exceeded")

            # Try to read data
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: self.process.read_nonblocking(size=4096, timeout=0.05),
                    ),
                    timeout=self.config.read_timeout,
                )
            except asyncio.TimeoutError:
                # Check inactivity timeout
                if (
                    current_time - last_output_time > self.config.inactivity_timeout
                    and accumulated_output
                ):
                    # Assume complete if we haven't received data in a while
                    logger.info("Inactivity timeout reached, assuming response complete")
                    break
                continue

            if data:
                last_output_time = current_time
                logger.debug(
                    f"PTY output: {data[:100]}..." if len(data) > 100 else f"PTY output: {data}"
                )

                # Parse each line
                for line in data.split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    msg = self.parser.parse_line(line)
                    if not msg:
                        continue

                    # Track session ID
                    if msg.session_id:
                        session_id = msg.session_id

                    # Accumulate content
                    if msg.type == "assistant" and msg.content:
                        accumulated_output += msg.content

                    # Track detailed content
                    if msg.detailed_content:
                        accumulated_detailed += msg.detailed_content

                    # Track result metadata
                    if msg.type == "result":
                        cost_usd = msg.cost_usd
                        duration_ms = msg.duration_ms
                        if msg.content:
                            accumulated_output = msg.content
                        if msg.detailed_content:
                            accumulated_detailed = msg.detailed_content
                        is_complete = True

                    # Track errors
                    if msg.type == "error":
                        error_msg = msg.content
                        is_complete = True

                    # Call chunk callback
                    if on_chunk:
                        await on_chunk(msg)

                    if msg.is_final:
                        is_complete = True

            await asyncio.sleep(0.01)

        return SessionResponse(
            output=accumulated_output.strip(),
            detailed_output=accumulated_detailed.strip(),
            session_id=session_id,
            success=not error_msg,
            error=error_msg,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    async def interrupt(self) -> bool:
        """Send Ctrl+C to interrupt the current operation."""
        if self.process.is_alive():
            self.process.sendcontrol("c")
            self.last_activity = datetime.now()
            logger.info(f"Sent interrupt to PTY session {self.session_id}")
            return True
        return False

    async def stop(self) -> None:
        """Stop the session gracefully."""
        await self._set_state(SessionState.STOPPING)
        await self.process.terminate()
        await self._set_state(SessionState.STOPPED)
        logger.info(
            f"PTY session {self.session_id} stopped "
            f"(duration: {(datetime.now() - self.created_at).total_seconds():.1f}s)"
        )

    def is_alive(self) -> bool:
        """Check if the session process is still running."""
        return self.process.is_alive()

    @property
    def pid(self) -> Optional[int]:
        """Get the process ID of the Codex process."""
        return self.process.pid

    async def _set_state(self, new_state: SessionState) -> None:
        """Update session state and notify callback."""
        old_state = self.state
        self.state = new_state
        if old_state != new_state:
            logger.debug(f"PTY session {self.session_id}: {old_state.value} -> {new_state.value}")
        if self.on_state_change:
            await self.on_state_change(new_state)
