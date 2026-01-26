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
    has_pending_question: bool = False  # True if terminated due to AskUserQuestion
    has_pending_plan_approval: bool = False  # True if terminated due to ExitPlanMode


async def _terminate_process_safely(
    process: asyncio.subprocess.Process,
    timeout: float = 5.0,
) -> None:
    """Terminate a process safely, falling back to kill if needed.

    Args:
        process: The process to terminate
        timeout: Seconds to wait for graceful termination before kill
    """
    if process.returncode is not None:
        return  # Already terminated

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # Process didn't terminate gracefully, force kill
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Process did not respond to kill signal")


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
        self._lock: asyncio.Lock = asyncio.Lock()
        self.db = db
        # Track ExitPlanMode for retry logic and early termination
        self._exit_plan_mode_tool_id: Optional[str] = None
        self._exit_plan_mode_error_detected: bool = False
        self._exit_plan_mode_detected: bool = False  # For early termination to show approval UI
        # Track AskUserQuestion for early termination
        self._ask_user_question_detected: bool = False
        # Track Plan subagent (Task tool with subagent_type=Plan) for plan approval
        self._plan_subagent_tool_id: Optional[str] = None
        self._plan_subagent_completed: bool = False

    async def _get_current_permission_mode(
        self, db_session_id: Optional[int], fallback_mode: Optional[str]
    ) -> str:
        """Get the current permission mode from the database.

        This allows detecting mode changes made via /mode command during execution.
        For example, if user switches to plan mode mid-execution, we can pick it up.

        Args:
            db_session_id: Database session ID to check
            fallback_mode: Mode to return if DB lookup fails

        Returns:
            Current permission mode from DB, or fallback_mode if not available
        """
        if not self.db or not db_session_id:
            return fallback_mode or config.CLAUDE_PERMISSION_MODE

        session = await self.db.get_session_by_id(db_session_id)
        if session and session.permission_mode:
            return session.permission_mode

        return fallback_mode or config.CLAUDE_PERMISSION_MODE

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
        _is_retry_after_exit_plan_error: bool = False,
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

        # Reset ExitPlanMode detection for this execution
        # Always reset these flags so each execution starts fresh
        self._exit_plan_mode_tool_id = None
        self._exit_plan_mode_error_detected = False
        self._exit_plan_mode_detected = False
        # Reset AskUserQuestion detection
        self._ask_user_question_detected = False
        # Reset Plan subagent detection
        self._plan_subagent_tool_id = None
        self._plan_subagent_completed = False

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
        async with self._lock:
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
                                    # Mark for early termination to handle question in Slack
                                    self._ask_user_question_detected = True
                                    logger.info(f"{log_prefix}AskUserQuestion detected - will terminate for Slack handling")
                                elif tool_name == "ExitPlanMode":
                                    self._exit_plan_mode_tool_id = block.get("id")
                                    # Check current mode from DB - user may have switched to plan mode
                                    # during execution via /mode command
                                    current_mode = await self._get_current_permission_mode(
                                        db_session_id, permission_mode
                                    )
                                    # Mark for early termination to handle approval in Slack
                                    if current_mode == "plan":
                                        self._exit_plan_mode_detected = True
                                        logger.info(
                                            f"{log_prefix}Tool: ExitPlanMode - will terminate for Slack approval (mode={current_mode})"
                                        )
                                    else:
                                        logger.info(f"{log_prefix}Tool: ExitPlanMode (mode={current_mode}, no approval needed)")
                                elif tool_name == "Task":
                                    # Track Task tool with Plan subagent for plan approval
                                    subagent_type = tool_input.get("subagent_type", "")
                                    if subagent_type == "Plan":
                                        # Check current mode from DB - user may have switched to plan mode
                                        current_mode = await self._get_current_permission_mode(
                                            db_session_id, permission_mode
                                        )
                                        if current_mode == "plan":
                                            self._plan_subagent_tool_id = block.get("id")
                                            logger.info(
                                                f"{log_prefix}Tool: Task (Plan subagent) - will trigger approval on completion (mode={current_mode})"
                                            )
                                        else:
                                            desc = tool_input.get("description", "")[:50]
                                            logger.info(f"{log_prefix}Tool: Task (Plan subagent) '{desc}...' (mode={current_mode}, no approval)")
                                    else:
                                        desc = tool_input.get("description", "")[:50]
                                        logger.info(f"{log_prefix}Tool: Task '{desc}...'")
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
                            # Note: _exit_plan_mode_detected is only set when mode was "plan" at
                            # tool detection time (checked via DB), so we check that flag here
                            # instead of permission_mode to support dynamic mode switching
                            if (
                                is_error
                                and self._exit_plan_mode_tool_id
                                and tool_use_id.startswith(self._exit_plan_mode_tool_id[:8])
                                and self._exit_plan_mode_detected
                                and not _is_retry_after_exit_plan_error
                            ):
                                logger.warning(
                                    f"{log_prefix}ExitPlanMode failed - will retry with bypass mode"
                                )
                                self._exit_plan_mode_error_detected = True

                            # Detect Plan subagent completion - trigger plan approval
                            # Note: _plan_subagent_tool_id is only set when mode was "plan" at
                            # tool detection time (checked via DB), so no additional mode check needed
                            if (
                                not is_error
                                and self._plan_subagent_tool_id
                                and tool_use_id.startswith(self._plan_subagent_tool_id[:8])
                            ):
                                logger.info(
                                    f"{log_prefix}Plan subagent completed - will trigger Slack approval"
                                )
                                self._plan_subagent_completed = True
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
                    await _terminate_process_safely(process)
                    break  # Exit the message processing loop

                # If AskUserQuestion detected, terminate early to handle in Slack
                # This must happen before Claude CLI returns the error to Claude
                if self._ask_user_question_detected:
                    logger.info(f"{log_prefix}Terminating execution to handle AskUserQuestion in Slack")
                    await _terminate_process_safely(process)
                    break  # Exit the message processing loop

                # If ExitPlanMode detected in plan mode, terminate early to show approval UI
                # The CLI would otherwise block waiting for interactive approval
                if self._exit_plan_mode_detected:
                    logger.info(f"{log_prefix}Terminating execution to handle plan approval in Slack")
                    await _terminate_process_safely(process)
                    break  # Exit the message processing loop

                # If Plan subagent completed in plan mode, terminate early to show approval UI
                # This handles the case where Claude uses Task(subagent_type=Plan) instead of ExitPlanMode
                if self._plan_subagent_completed:
                    logger.info(f"{log_prefix}Terminating execution to handle Plan subagent approval in Slack")
                    await _terminate_process_safely(process)
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
            # Note: _exit_plan_mode_error_detected is only set when mode was "plan" at tool
            # detection time, so no need to re-check permission_mode here
            if self._exit_plan_mode_error_detected and not _is_retry_after_exit_plan_error:
                logger.info(
                    f"{log_prefix}Retrying execution with bypass mode after ExitPlanMode error (depth={_recursion_depth + 1})"
                )

                return await self.execute(
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
                    _is_retry_after_exit_plan_error=True,  # Prevent infinite retry
                )

            # Plan approval is triggered by either ExitPlanMode or Plan subagent completion
            has_plan_approval = self._exit_plan_mode_detected or self._plan_subagent_completed

            return ExecutionResult(
                success=success,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=error_msg,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                has_pending_question=self._ask_user_question_detected,
                has_pending_plan_approval=has_plan_approval,
            )

        except asyncio.CancelledError:
            await _terminate_process_safely(process)
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
            await _terminate_process_safely(process)
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=str(e),
            )
        finally:
            async with self._lock:
                self._active_processes.pop(track_id, None)

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        async with self._lock:
            process = self._active_processes.get(execution_id)
        if process:
            process.terminate()
            return True
        return False

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        async with self._lock:
            processes = list(self._active_processes.values())
            self._active_processes.clear()
        for process in processes:
            process.terminate()
        return len(processes)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
