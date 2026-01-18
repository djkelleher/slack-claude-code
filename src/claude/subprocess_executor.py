"""Claude Code executor using subprocess with stream-json output."""

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from loguru import logger

from ..config import config
from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from ..database.repository import DatabaseRepository

# UUID pattern for validating session IDs
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
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
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.db = db
        # Track ExitPlanMode for retry logic
        self._exit_plan_mode_tool_id: Optional[str] = None
        self._exit_plan_mode_error_detected: bool = False
        self._is_retry_after_exit_plan_error: bool = False

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        permission_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        _recursion_depth: int = 0,
    ) -> ExecutionResult:
        """Execute a prompt via Claude Code subprocess.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Identifier for this execution (for tracking)
            resume_session_id: Claude session ID to resume (from previous execution)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message
            permission_mode: Permission mode to use (overrides config default)
            db_session_id: Database session ID for smart context tracking (optional)
            model: Model to use (e.g., "opus", "sonnet", "haiku")
            _recursion_depth: Internal parameter to track retry depth (max 3)

        Returns:
            ExecutionResult with the command output
        """
        # Create log prefix for this session
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""

        # Prevent infinite recursion (max 3 retries)
        MAX_RECURSION_DEPTH = 3
        if _recursion_depth >= MAX_RECURSION_DEPTH:
            logger.error(
                f"{log_prefix}Max recursion depth ({MAX_RECURSION_DEPTH}) reached, aborting"
            )
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max retry depth ({MAX_RECURSION_DEPTH}) exceeded",
            )

        # Reset ExitPlanMode error detection for this execution
        # Always reset these flags so each execution starts fresh
        self._exit_plan_mode_tool_id = None
        self._exit_plan_mode_error_detected = False
        # Note: _is_retry_after_exit_plan_error is preserved during retry to prevent infinite loops

        # Build command
        cmd = [
            "claude",
            "-p",
            "--verbose",  # Required for stream-json
            "--output-format",
            "stream-json",
        ]

        # Add model flag if specified
        if model:
            cmd.extend(["--model", model])
            logger.info(f"{log_prefix}Using --model {model}")

        # Determine permission mode: explicit > config default
        mode = permission_mode or config.CLAUDE_PERMISSION_MODE
        if mode in config.VALID_PERMISSION_MODES:
            cmd.extend(["--permission-mode", mode])
            logger.info(f"{log_prefix}Using --permission-mode {mode}")
        else:
            logger.warning(f"{log_prefix}Invalid permission mode: {mode}, using {config.DEFAULT_BYPASS_MODE}")
            cmd.extend(["--permission-mode", config.DEFAULT_BYPASS_MODE])

        # Add allowed tools restriction if configured
        if config.ALLOWED_TOOLS:
            cmd.extend(["--allowed-tools", config.ALLOWED_TOOLS])
            logger.info(f"{log_prefix}Using --allowed-tools {config.ALLOWED_TOOLS}")

        # In plan mode, inject instructions to ask clarifying questions
        if mode == "plan":
            plan_instructions = (
                "When in plan mode, you MUST ask clarifying questions using the AskUserQuestion tool "
                "before creating your implementation plan. Do not proceed to write the plan until you "
                "have gathered enough information from the user about their requirements, preferences, "
                "and constraints. Ask 2-4 focused questions to understand the scope and approach."
            )
            # cmd.extend(["--append-system-prompt", plan_instructions])
            # logger.info(f"{log_prefix}Added plan mode question instructions")

        # Add resume flag if we have a valid Claude session ID (must be UUID format)
        if resume_session_id and UUID_PATTERN.match(resume_session_id):
            cmd.extend(["--resume", resume_session_id])
            logger.info(f"{log_prefix}Resuming session {resume_session_id}")
        elif resume_session_id:
            logger.warning(f"{log_prefix}Invalid session ID format (not UUID): {resume_session_id}")

        # Add the prompt
        cmd.append(prompt)

        # Log full command with all flags, but truncate prompt for readability
        cmd_without_prompt = " ".join(cmd[:-1])
        prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"{log_prefix}Executing: {cmd_without_prompt} '{prompt_preview}'")

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
            logger.error(f"{log_prefix}Failed to start Claude process: {e}")
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
                line = await process.stdout.readline()

                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
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
                        preview = (
                            msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                        )
                        logger.debug(f"{log_prefix}Claude: {preview}")
                    # Log tool use and track file context
                    if msg.raw:
                        message = msg.raw.get("message", {})
                        for block in message.get("content", []):
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                # Log tool use summary and track file operations
                                if tool_name in ("Read", "Edit", "Write"):
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(f"{log_prefix}Tool: {tool_name} {file_path}")
                                elif tool_name == "Bash":
                                    command = tool_input.get("command", "")[:50]
                                    logger.info(f"{log_prefix}Tool: Bash '{command}...'")
                                elif tool_name == "AskUserQuestion":
                                    questions = tool_input.get("questions", [])
                                    if questions:
                                        first_q = questions[0].get("question", "?")[:80]
                                        logger.info(
                                            f"{log_prefix}Tool: AskUserQuestion - '{first_q}...' ({len(questions)} question(s))"
                                        )
                                    else:
                                        logger.info(f"{log_prefix}Tool: AskUserQuestion")
                                elif tool_name == "ExitPlanMode":
                                    self._exit_plan_mode_tool_id = block.get("id")
                                    logger.info(f"{log_prefix}Tool: ExitPlanMode")
                                else:
                                    logger.info(f"{log_prefix}Tool: {tool_name}")
                elif msg.type == "user" and msg.raw:
                    # Log tool results summary
                    message = msg.raw.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")[:8]
                            is_error = block.get("is_error", False)
                            status = "ERROR" if is_error else "OK"
                            logger.info(f"{log_prefix}Tool result [{tool_use_id}]: {status}")

                            # Detect ExitPlanMode ERROR for immediate retry
                            if (
                                is_error
                                and self._exit_plan_mode_tool_id
                                and tool_use_id.startswith(self._exit_plan_mode_tool_id[:8])
                                and permission_mode == "plan"
                                and not self._is_retry_after_exit_plan_error
                            ):
                                logger.warning(
                                    f"{log_prefix}ExitPlanMode failed - will retry with bypass mode"
                                )
                                self._exit_plan_mode_error_detected = True
                elif msg.type == "init":
                    logger.info(f"{log_prefix}Session initialized: {msg.session_id}")
                elif msg.type == "error":
                    logger.error(f"{log_prefix}Error: {msg.content}")
                elif msg.type == "result":
                    if msg.cost_usd:
                        logger.info(
                            f"{log_prefix}Claude Finished - completed in {msg.duration_ms}ms, cost ${msg.cost_usd:.4f}"
                        )
                    else:
                        logger.info(
                            f"{log_prefix}Claude Finished - completed in {msg.duration_ms}ms"
                        )

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
                    # Check for errors in result message (e.g., session not found)
                    if msg.raw and msg.raw.get("is_error"):
                        errors = msg.raw.get("errors", [])
                        if errors:
                            error_msg = "; ".join(errors)
                            logger.warning(f"{log_prefix}Result contains errors: {error_msg}")

                # Track errors from error-type messages
                if msg.type == "error":
                    error_msg = msg.content

                # Call chunk callback
                if on_chunk:
                    await on_chunk(msg)

                # If ExitPlanMode error detected, terminate early and retry
                if self._exit_plan_mode_error_detected:
                    logger.info(f"{log_prefix}Terminating execution to retry without plan mode")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        process.kill()
                    break  # Exit the message processing loop

                if msg.is_final:
                    break

            # Wait for process to complete
            await process.wait()

            # Check stderr for errors
            stderr = await process.stderr.read()
            if stderr:
                stderr_str = stderr.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning(f"{log_prefix}Claude stderr: {stderr_str}")
                    # Only treat stderr as error if process failed
                    if process.returncode != 0 and not error_msg:
                        error_msg = stderr_str

            success = process.returncode == 0 and not error_msg

            # Check if session not found - retry without resume
            if (
                not success
                and resume_session_id
                and "No conversation found with session ID" in (error_msg or "")
            ):
                logger.info(
                    f"{log_prefix}Session {resume_session_id} not found, retrying without resume (depth={_recursion_depth + 1})"
                )
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,  # Don't resume
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=permission_mode,
                    db_session_id=db_session_id,
                    model=model,
                    _recursion_depth=_recursion_depth + 1,
                )

            # Check if ExitPlanMode error detected - retry without plan mode
            if (
                self._exit_plan_mode_error_detected
                and permission_mode == "plan"
                and not self._is_retry_after_exit_plan_error
            ):
                logger.info(
                    f"{log_prefix}Retrying execution with bypass mode after ExitPlanMode error (depth={_recursion_depth + 1})"
                )

                # Prevent infinite retry loop
                self._is_retry_after_exit_plan_error = True

                result = await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=resume_session_id,  # Keep the session
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=config.DEFAULT_BYPASS_MODE,  # Switch to bypass mode
                    db_session_id=db_session_id,
                    model=model,
                    _recursion_depth=_recursion_depth + 1,
                )

                # Reset retry flag after completion so future executions work normally
                self._is_retry_after_exit_plan_error = False

                return result

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
            logger.error(f"{log_prefix}Error during execution: {e}")
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
