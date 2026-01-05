"""Claude Code executor using subprocess with stream-json output.

This is more reliable than PTY interaction for Claude Code's TUI.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

from ..config import config
from .streaming import StreamMessage, StreamParser

logger = logging.getLogger(__name__)

# UUID pattern for validating session IDs
UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


@dataclass
class ExecutionResult:
    """Result of a Claude CLI execution."""

    success: bool
    output: str
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False


class SubprocessExecutor:
    """Execute Claude Code via subprocess with stream-json output.

    Uses `claude -p --output-format stream-json` for reliable non-interactive execution.
    Supports session resume via --resume flag.
    """

    def __init__(self, timeout: int = None) -> None:
        self.timeout = timeout or config.timeouts.execution.command
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
    ) -> ExecutionResult:
        """Execute a prompt via Claude Code subprocess.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Identifier for this execution (for tracking)
            resume_session_id: Claude session ID to resume (from previous execution)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message

        Returns:
            ExecutionResult with the command output
        """
        # Build command
        cmd = [
            "claude",
            "-p",
            "--verbose",  # Required for stream-json
            "--output-format", "stream-json",
        ]

        # Add resume flag if we have a valid Claude session ID (must be UUID format)
        if resume_session_id and UUID_PATTERN.match(resume_session_id):
            cmd.extend(["--resume", resume_session_id])
            logger.info(f"Resuming session {resume_session_id}")
        elif resume_session_id:
            logger.warning(f"Invalid session ID format (not UUID): {resume_session_id}")

        # Add the prompt
        cmd.append(prompt)

        logger.info(f"Executing: {' '.join(cmd[:5])}... (prompt: {prompt[:50]}...)")

        # Start subprocess with increased line limit (default is 64KB)
        # Large files can produce JSON lines exceeding this limit
        limit = 200 * 1024 * 1024  # 200MB limit for large file reads
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"Failed to start Claude process: {e}")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to start Claude: {e}",
            )

        # Track process for cancellation
        track_id = execution_id or session_id or "default"
        self._active_processes[track_id] = process

        parser = StreamParser()
        accumulated_output = ""
        result_session_id = None
        cost_usd = None
        duration_ms = None
        error_msg = None

        try:
            # Read stdout line by line
            while True:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout waiting for Claude output")
                    process.terminate()
                    await process.wait()  # Prevent zombie process
                    return ExecutionResult(
                        success=False,
                        output=accumulated_output,
                        session_id=result_session_id,
                        error="Command timed out",
                    )

                if not line:
                    break

                line_str = line.decode('utf-8', errors='replace').strip()
                if not line_str:
                    continue

                # Print raw output for debugging
                print(line_str, flush=True)

                # Parse the JSON message
                msg = parser.parse_line(line_str)
                if not msg:
                    continue

                # Track session ID
                if msg.session_id:
                    result_session_id = msg.session_id

                # Accumulate content
                if msg.type == "assistant" and msg.content:
                    accumulated_output += msg.content

                # Track result metadata
                if msg.type == "result":
                    cost_usd = msg.cost_usd
                    duration_ms = msg.duration_ms
                    if msg.session_id:
                        result_session_id = msg.session_id

                # Track errors
                if msg.type == "error":
                    error_msg = msg.content

                # Call chunk callback
                if on_chunk:
                    await on_chunk(msg)

                if msg.is_final:
                    break

            # Wait for process to complete
            await process.wait()

            # Check stderr for errors
            stderr = await process.stderr.read()
            if stderr:
                stderr_str = stderr.decode('utf-8', errors='replace').strip()
                if stderr_str:
                    logger.warning(f"Claude stderr: {stderr_str}")
                    if not error_msg:
                        error_msg = stderr_str

            success = process.returncode == 0 and not error_msg

            return ExecutionResult(
                success=success,
                output=accumulated_output,
                session_id=result_session_id,
                error=error_msg,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
            )

        except asyncio.CancelledError:
            process.terminate()
            await process.wait()  # Prevent zombie process
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                session_id=result_session_id,
                error="Cancelled",
                was_cancelled=True,
            )
        except Exception as e:
            logger.error(f"Error during execution: {e}")
            process.terminate()
            await process.wait()  # Prevent zombie process
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                session_id=result_session_id,
                error=str(e),
            )
        finally:
            self._active_processes.pop(track_id, None)

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        process = self._active_processes.get(execution_id)
        if process:
            process.terminate()
            return True
        return False

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        count = 0
        for process in list(self._active_processes.values()):
            process.terminate()
            count += 1
        self._active_processes.clear()
        return count

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
