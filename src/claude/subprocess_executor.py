"""Claude Code executor using subprocess with stream-json output.

This is more reliable than PTY interaction for Claude Code's TUI.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Optional

from ..config import config
from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from ..database.repository import DatabaseRepository

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
    detailed_output: str = ""  # Full output with tool use details
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

    def __init__(
        self,
        timeout: int = None,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        self.timeout = timeout or config.timeouts.execution.command
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._background_tasks: set[asyncio.Task] = set()  # Keep references to prevent GC
        self.db = db  # Optional database for smart context tracking

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        plan_mode: bool = False,
        db_session_id: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute a prompt via Claude Code subprocess.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Identifier for this execution (for tracking)
            resume_session_id: Claude session ID to resume (from previous execution)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message
            plan_mode: If True, use --permission-mode plan for planning phase
            db_session_id: Database session ID for smart context tracking (optional)

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

        # Add permission mode
        if plan_mode:
            # Use plan mode for planning phase
            cmd.extend(["--permission-mode", "plan"])
            logger.info("Using --permission-mode plan for planning phase")
        else:
            # Use standard permission mode
            if config.CLAUDE_PERMISSION_MODE in ["approve-all", "prompt", "deny"]:
                cmd.extend(["--permissions", config.CLAUDE_PERMISSION_MODE])
            else:
                logger.warning(f"Invalid CLAUDE_PERMISSION_MODE: {config.CLAUDE_PERMISSION_MODE}, using approve-all")
                cmd.extend(["--permissions", "approve-all"])

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
        accumulated_detailed = ""
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
                    logger.warning("Timeout waiting for Claude output")
                    process.terminate()
                    await process.wait()  # Prevent zombie process
                    return ExecutionResult(
                        success=False,
                        output=accumulated_output,
                        detailed_output=accumulated_detailed,
                        session_id=result_session_id,
                        error="Command timed out",
                    )

                if not line:
                    break

                line_str = line.decode('utf-8', errors='replace').strip()
                if not line_str:
                    continue

                # Parse the JSON message
                msg = parser.parse_line(line_str)
                if not msg:
                    continue

                # Log human-readable summaries (not full JSON)
                if msg.type == "assistant":
                    # Log text content
                    if msg.content:
                        preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                        logger.debug(f"Claude: {preview}")
                    # Log tool use and track file context
                    if msg.raw:
                        message = msg.raw.get("message", {})
                        for block in message.get("content", []):
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                # Log tool use summary and track file operations
                                if tool_name == "Read":
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(f"Tool: Read {file_path}")
                                    self._track_file_context(db_session_id, file_path, "read")
                                elif tool_name == "Edit":
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(f"Tool: Edit {file_path}")
                                    self._track_file_context(db_session_id, file_path, "modified")
                                elif tool_name == "Write":
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(f"Tool: Write {file_path}")
                                    self._track_file_context(db_session_id, file_path, "created")
                                elif tool_name == "Bash":
                                    command = tool_input.get("command", "")[:50]
                                    logger.info(f"Tool: Bash '{command}...'")
                                else:
                                    logger.info(f"Tool: {tool_name}")
                elif msg.type == "user" and msg.raw:
                    # Log tool results summary
                    message = msg.raw.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")[:8]
                            is_error = block.get("is_error", False)
                            status = "ERROR" if is_error else "OK"
                            logger.info(f"Tool result [{tool_use_id}]: {status}")
                elif msg.type == "init":
                    logger.info(f"Session initialized: {msg.session_id}")
                elif msg.type == "error":
                    logger.error(f"Error: {msg.content}")
                elif msg.type == "result":
                    logger.info(f"Completed in {msg.duration_ms}ms, cost ${msg.cost_usd:.4f}" if msg.cost_usd else f"Completed in {msg.duration_ms}ms")

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
                    # Get final accumulated detailed output
                    if msg.detailed_content:
                        accumulated_detailed = msg.detailed_content

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
                detailed_output=accumulated_detailed,
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
                detailed_output=accumulated_detailed,
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
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=str(e),
            )
        finally:
            self._active_processes.pop(track_id, None)

    def _track_file_context(
        self, db_session_id: Optional[int], file_path: str, context_type: str
    ) -> None:
        """Track file context usage in background (non-blocking).

        Args:
            db_session_id: Database session ID for file context tracking
            file_path: Path to the file being accessed
            context_type: Type of access ("read", "modified", "created")
        """
        if not self.db or not db_session_id or not file_path:
            return

        # Queue context update (async, non-blocking)
        async def _do_track():
            try:
                await self.db.track_file_context(db_session_id, file_path, context_type)
            except Exception as e:
                logger.warning(f"Failed to track file context for {file_path}: {e}")

        task = asyncio.create_task(_do_track())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        # Wait for any pending background tasks (file context tracking)
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
