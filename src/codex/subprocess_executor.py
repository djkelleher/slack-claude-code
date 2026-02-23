"""Codex CLI executor using subprocess with stream-json output."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from loguru import logger

from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config, parse_model_effort

from src.claude.streaming import _concat_with_spacing

from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from src.database.repository import DatabaseRepository


@dataclass
class ExecutionResult:
    """Result of a Codex CLI execution."""

    success: bool
    output: str
    detailed_output: str = ""  # Full output with tool use details
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False


class SubprocessExecutor:
    """Execute Codex CLI via subprocess with stream-json output.

    Uses `codex exec --json` for reliable non-interactive execution.
    Supports session resume via `codex exec resume` command.
    """

    def __init__(
        self,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_channels: dict[str, str] = {}  # track_id -> channel_id
        self._lock: asyncio.Lock = asyncio.Lock()
        self.db = db

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        _recursion_depth: int = 0,
    ) -> ExecutionResult:
        """Execute a prompt via Codex CLI subprocess.

        Args:
            prompt: The prompt to send to Codex
            working_directory: Directory to run Codex in
            session_id: Identifier for this execution (for tracking)
            resume_session_id: Codex session ID to resume (from previous execution)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message
            sandbox_mode: Sandbox mode (read-only, workspace-write, danger-full-access)
            approval_mode: Approval mode (untrusted, on-request, never)
            db_session_id: Database session ID for tracking (optional)
            model: Model to use (e.g., "gpt-5.3-codex", "gpt-5.2")
            channel_id: Slack channel ID (for process tracking)
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

        # Build command - use codex exec for non-interactive execution
        if resume_session_id:
            # Resume existing session
            cmd = [
                "codex",
                "exec",
                "resume",
                resume_session_id,
                "--json",  # Stream JSON events
            ]
            # Add the prompt as follow-up
            if prompt:
                cmd.append(prompt)
            logger.info(f"{log_prefix}Resuming session {resume_session_id}")
        else:
            # New execution
            cmd = [
                "codex",
                "exec",
                "--json",  # Stream JSON events
            ]

        # Add model flag if specified, parsing out effort suffix
        effort = None
        if model:
            base_model, effort = parse_model_effort(model)
            cmd.extend(["--model", base_model])
            logger.info(f"{log_prefix}Using --model {base_model}")
            if effort:
                cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
                logger.info(f"{log_prefix}Using reasoning effort: {effort}")

        # Determine sandbox mode: explicit > session > config default
        mode = sandbox_mode or config.CODEX_SANDBOX_MODE
        if mode in config.VALID_SANDBOX_MODES:
            cmd.extend(["--sandbox", mode])
            logger.info(f"{log_prefix}Using --sandbox {mode}")
        else:
            logger.warning(
                f"{log_prefix}Invalid sandbox mode: {mode}, using {config.CODEX_SANDBOX_MODE}"
            )
            cmd.extend(["--sandbox", config.CODEX_SANDBOX_MODE])

        # Determine approval mode: explicit > session > config default
        # Codex CLI doesn't support --ask-for-approval; map to equivalent flags
        approval_raw = approval_mode or config.CODEX_APPROVAL_MODE
        approval = normalize_codex_approval_mode(approval_raw)
        if approval != (approval_raw or "").strip().lower():
            logger.warning(
                f"{log_prefix}Deprecated approval mode `{approval_raw}` mapped to `{approval}`"
            )
        if approval == "never":
            cmd.append("--full-auto")
            logger.info(f"{log_prefix}Using --full-auto (approval=never)")
        else:
            logger.info(f"{log_prefix}Approval mode: {approval} (no extra flag needed)")

        # Add working directory
        cmd.extend(["--cd", working_directory])

        # Add the prompt (for new executions)
        if not resume_session_id and prompt:
            cmd.append(prompt)

        # Log full command with all flags, but truncate prompt for readability
        cmd_preview = " ".join(cmd[:-1] if prompt else cmd)
        prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"{log_prefix}Executing: {cmd_preview} '{prompt_preview}'")

        # Start subprocess with increased line limit
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
            logger.error(f"{log_prefix}Failed to start Codex process: {e}")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to start Codex: {e}",
            )

        # Track process for cancellation
        track_id = execution_id or session_id or "default"
        if channel_id:
            track_id = f"{channel_id}_{track_id}"
        async with self._lock:
            self._active_processes[track_id] = process
            if channel_id:
                self._process_channels[track_id] = channel_id

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

                # Log human-readable summaries
                if msg.type == "assistant":
                    if msg.content:
                        preview = (
                            msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                        )
                        logger.debug(f"{log_prefix}Codex: {preview}")
                elif msg.type == "tool_call":
                    for tool in msg.tool_activities:
                        logger.info(f"{log_prefix}Tool: {tool.name} {tool.input_summary}")
                elif msg.type == "tool_result":
                    for tool in msg.tool_activities:
                        status = "ERROR" if tool.is_error else "OK"
                        logger.info(
                            f"{log_prefix}Tool result [{tool.id[:8] if tool.id else '?'}]: {status}"
                        )
                elif msg.type == "init":
                    logger.info(f"{log_prefix}Session initialized: {msg.session_id}")
                elif msg.type == "error":
                    logger.error(f"{log_prefix}Error: {msg.content}")
                elif msg.type == "result":
                    if msg.cost_usd:
                        logger.info(
                            f"{log_prefix}Codex Finished - completed in {msg.duration_ms}ms, cost ${msg.cost_usd:.4f}"
                        )
                    else:
                        logger.info(
                            f"{log_prefix}Codex Finished - completed in {msg.duration_ms}ms"
                        )

                # Track session ID
                if msg.session_id:
                    result_session_id = msg.session_id

                # Accumulate content
                if msg.type == "assistant" and msg.content:
                    accumulated_output = _concat_with_spacing(
                        accumulated_output, msg.content
                    )

                # Track result metadata
                if msg.type == "result":
                    cost_usd = msg.cost_usd
                    duration_ms = msg.duration_ms
                    if msg.session_id:
                        result_session_id = msg.session_id
                    if msg.detailed_content:
                        accumulated_detailed = msg.detailed_content
                    # Check for errors in result message
                    if msg.raw and msg.raw.get("is_error"):
                        errors = msg.raw.get("errors", [])
                        if errors:
                            error_msg = "; ".join(str(e) for e in errors)
                            logger.warning(f"{log_prefix}Result contains errors: {error_msg}")

                # Track errors from error-type messages
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
                stderr_str = stderr.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning(f"{log_prefix}Codex stderr: {stderr_str}")
                    # Only treat stderr as error if process failed
                    if process.returncode != 0 and not error_msg:
                        error_msg = stderr_str

            success = process.returncode == 0 and not error_msg

            # Check if session not found - retry without resume
            if (
                not success
                and resume_session_id
                and (
                    "session not found" in (error_msg or "").lower()
                    or "no conversation found" in (error_msg or "").lower()
                )
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
                    sandbox_mode=sandbox_mode,
                    approval_mode=approval_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    _recursion_depth=_recursion_depth + 1,
                )

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
            await process.wait()
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
            await process.wait()
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
                self._process_channels.pop(track_id, None)

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        async with self._lock:
            process = self._active_processes.get(execution_id)
        if process:
            process.terminate()
            return True
        return False

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel.

        Args:
            channel_id: The Slack channel ID to cancel executions for

        Returns:
            Number of processes cancelled
        """
        async with self._lock:
            track_ids_to_cancel = [
                track_id
                for track_id, ch_id in self._process_channels.items()
                if ch_id == channel_id
            ]
            processes = []
            for track_id in track_ids_to_cancel:
                if track_id in self._active_processes:
                    processes.append(self._active_processes.pop(track_id))
                    self._process_channels.pop(track_id, None)

        for process in processes:
            process.terminate()
        return len(processes)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        async with self._lock:
            processes = list(self._active_processes.values())
            self._active_processes.clear()
            self._process_channels.clear()
        for process in processes:
            process.terminate()
        return len(processes)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
