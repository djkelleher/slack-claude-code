"""Codex CLI executor using subprocess with stream-json output."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
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
        on_user_input_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        permission_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        _recursion_depth: int = 0,
        _force_legacy: bool = False,
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
            on_user_input_request: Callback for native app-server request_user_input prompts
            permission_mode: Session mode (plan/default/etc)
            _recursion_depth: Internal parameter to track retry depth (max 3)
            _force_legacy: Internal flag to skip native app-server path

        Returns:
            ExecutionResult with the command output
        """
        # Create log prefix for this session
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        effective_prompt = self._build_effective_prompt(prompt, log_prefix)

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

        plan_mode_active = (permission_mode or "").strip().lower() == "plan"
        use_native_plan_mode = (
            config.CODEX_NATIVE_PLAN_MODE_ENABLED and plan_mode_active and not _force_legacy
        )
        if use_native_plan_mode:
            try:
                return await self._execute_via_app_server(
                    prompt=prompt,
                    effective_prompt=effective_prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=resume_session_id,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    on_user_input_request=on_user_input_request,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"{log_prefix}Native app-server plan mode failed ({type(e).__name__}: {e}); "
                    "falling back to legacy `codex exec --json`"
                )

        return await self._execute_legacy(
            prompt=prompt,
            effective_prompt=effective_prompt,
            working_directory=working_directory,
            session_id=session_id,
            resume_session_id=resume_session_id,
            execution_id=execution_id,
            on_chunk=on_chunk,
            on_user_input_request=on_user_input_request,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            permission_mode=permission_mode,
            db_session_id=db_session_id,
            model=model,
            channel_id=channel_id,
            _recursion_depth=_recursion_depth,
            _force_legacy=_force_legacy,
        )

    async def _execute_legacy(
        self,
        prompt: str,
        effective_prompt: str,
        working_directory: str,
        session_id: Optional[str],
        resume_session_id: Optional[str],
        execution_id: Optional[str],
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]],
        on_user_input_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
        sandbox_mode: Optional[str],
        approval_mode: Optional[str],
        permission_mode: Optional[str],
        db_session_id: Optional[int],
        model: Optional[str],
        channel_id: Optional[str],
        _recursion_depth: int,
        _force_legacy: bool,
    ) -> ExecutionResult:
        """Execute using legacy `codex exec --json` flow."""
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""

        # Build command - use codex exec for non-interactive execution
        is_resume = bool(resume_session_id)
        if is_resume:
            # Resume existing session.
            # Important: codex exec resume requires options before positional args:
            # `codex exec resume [options] <session_id> <prompt>`
            cmd = ["codex"]
            if config.CODEX_USE_DANGEROUS_BYPASS:
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            cmd.extend(
                [
                    "exec",
                    "resume",
                    "--json",  # Stream JSON events
                ]
            )
            logger.info(f"{log_prefix}Resuming session {resume_session_id}")
        else:
            # New execution
            cmd = ["codex"]
            if config.CODEX_USE_DANGEROUS_BYPASS:
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            cmd.extend(
                [
                    "exec",
                    "--json",  # Stream JSON events
                ]
            )

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
        # `codex exec resume` does not support `--sandbox`, so only pass it for new executions.
        mode = sandbox_mode or config.CODEX_SANDBOX_MODE
        if config.CODEX_USE_DANGEROUS_BYPASS:
            logger.info(
                f"{log_prefix}Dangerous bypass enabled; not passing --sandbox (requested: {mode})"
            )
        else:
            if mode in config.VALID_SANDBOX_MODES:
                if is_resume:
                    logger.info(
                        f"{log_prefix}Resume mode active; not passing --sandbox (requested: {mode})"
                    )
                else:
                    cmd.extend(["--sandbox", mode])
                    logger.info(f"{log_prefix}Using --sandbox {mode}")
            else:
                logger.warning(
                    f"{log_prefix}Invalid sandbox mode: {mode}, using {config.CODEX_SANDBOX_MODE}"
                )
                if not is_resume:
                    cmd.extend(["--sandbox", config.CODEX_SANDBOX_MODE])

        # Determine approval mode: explicit > session > config default
        # Codex CLI doesn't support --ask-for-approval; map to equivalent flags
        approval_raw = approval_mode or config.CODEX_APPROVAL_MODE
        approval = normalize_codex_approval_mode(approval_raw)
        if approval != (approval_raw or "").strip().lower():
            logger.warning(
                f"{log_prefix}Deprecated approval mode `{approval_raw}` mapped to `{approval}`"
            )
        if config.CODEX_USE_DANGEROUS_BYPASS:
            logger.info(
                f"{log_prefix}Dangerous bypass enabled; ignoring approval mode `{approval}`"
            )
        elif approval == "never":
            cmd.append("--full-auto")
            logger.info(f"{log_prefix}Using --full-auto (approval=never)")
        else:
            logger.info(f"{log_prefix}Approval mode: {approval} (no extra flag needed)")

        # Add working directory.
        # `codex exec resume` does not support --cd; rely on subprocess cwd for resume.
        if is_resume:
            logger.info(f"{log_prefix}Resume mode uses process cwd: {working_directory}")
        else:
            cmd.extend(["--cd", working_directory])

        # Add positional args after options.
        if is_resume:
            resume_id = resume_session_id or ""
            cmd.append(resume_id)
            if effective_prompt:
                cmd.append(effective_prompt)
        elif effective_prompt:
            cmd.append(effective_prompt)

        # Log full command with all flags, but truncate prompt for readability
        if effective_prompt:
            cmd_preview = " ".join(cmd[:-1])
            prompt_preview = (
                effective_prompt[:100] + "..." if len(effective_prompt) > 100 else effective_prompt
            )
            logger.info(f"{log_prefix}Executing: {cmd_preview} '{prompt_preview}'")
        else:
            logger.info(f"{log_prefix}Executing: {' '.join(cmd)}")

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
                    if is_resume and msg.session_id == resume_session_id:
                        logger.info(f"{log_prefix}Session resumed: {msg.session_id}")
                    else:
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
                    accumulated_output = _concat_with_spacing(accumulated_output, msg.content)

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
                    on_user_input_request=on_user_input_request,
                    sandbox_mode=sandbox_mode,
                    approval_mode=approval_mode,
                    permission_mode=permission_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    _recursion_depth=_recursion_depth + 1,
                    _force_legacy=True,
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

    async def _execute_via_app_server(
        self,
        prompt: str,
        effective_prompt: str,
        working_directory: str,
        session_id: Optional[str],
        resume_session_id: Optional[str],
        execution_id: Optional[str],
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]],
        on_user_input_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
        db_session_id: Optional[int],
        model: Optional[str],
        channel_id: Optional[str],
    ) -> ExecutionResult:
        """Execute using Codex app-server JSON-RPC flow (native plan mode)."""
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        logger.info(f"{log_prefix}Executing via `codex app-server` native plan flow")

        # Native plan mode safety overrides.
        approval = "on-request"
        sandbox = "read-only"
        logger.info(
            f"{log_prefix}Native plan safety enforced: approval={approval}, sandbox={sandbox}"
        )

        cmd = ["codex", "app-server", "--listen", "stdio://"]
        limit = 200 * 1024 * 1024
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                limit=limit,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start codex app-server: {e}") from e

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
        result_session_id = resume_session_id
        cost_usd = None
        duration_ms = None
        error_msg = None
        started_at = time.monotonic()
        next_request_id = 1
        response_cache: dict[str, dict] = {}

        if model:
            base_model, effort = parse_model_effort(model)
        else:
            base_model, effort = None, None

        async def send_rpc(payload: dict) -> None:
            if process.stdin is None:
                raise RuntimeError("app-server stdin is unavailable")
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def send_request(method: str, params: dict) -> int:
            nonlocal next_request_id
            request_id = next_request_id
            next_request_id += 1
            await send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return request_id

        async def handle_stream_message(msg: StreamMessage) -> bool:
            nonlocal accumulated_output, accumulated_detailed, result_session_id
            nonlocal cost_usd, duration_ms, error_msg

            if msg.type == "assistant" and msg.content:
                preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
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
                duration_display = msg.duration_ms if msg.duration_ms is not None else "?"
                logger.info(f"{log_prefix}Codex Finished - completed in {duration_display}ms")

            if msg.session_id:
                result_session_id = msg.session_id

            if msg.type == "assistant" and msg.content:
                accumulated_output = _concat_with_spacing(accumulated_output, msg.content)

            if msg.type == "result":
                cost_usd = msg.cost_usd
                duration_ms = msg.duration_ms
                if msg.detailed_content:
                    accumulated_detailed = msg.detailed_content
                if msg.raw and msg.raw.get("is_error"):
                    errors = msg.raw.get("errors", [])
                    if errors:
                        error_msg = "; ".join(str(e) for e in errors)

            if msg.type == "error":
                error_msg = msg.content

            if on_chunk:
                await on_chunk(msg)

            return msg.is_final

        async def handle_notification(method: str, params: dict) -> bool:
            nonlocal result_session_id

            if method == "thread/started":
                thread = params.get("thread", {})
                thread_id = thread.get("id")
                if thread_id:
                    result_session_id = str(thread_id)
                    msg = parser.parse_line(
                        json.dumps({"type": "thread.started", "thread_id": str(thread_id)})
                    )
                    if msg:
                        return await handle_stream_message(msg)
                return False

            if method == "item/started":
                item = params.get("item", {})
                if item.get("type") == "commandExecution":
                    synthetic = {
                        "type": "item.started",
                        "item": {
                            "id": item.get("id"),
                            "type": "command_execution",
                            "command": item.get("command", ""),
                            "status": item.get("status", ""),
                        },
                    }
                    msg = parser.parse_line(json.dumps(synthetic))
                    if msg:
                        return await handle_stream_message(msg)
                return False

            if method == "item/completed":
                item = params.get("item", {})
                item_type = item.get("type")
                if item_type == "agentMessage":
                    synthetic = {
                        "type": "item.completed",
                        "item": {
                            "id": item.get("id"),
                            "type": "agent_message",
                            "text": item.get("text", ""),
                        },
                    }
                    msg = parser.parse_line(json.dumps(synthetic))
                    if msg:
                        return await handle_stream_message(msg)
                    return False

                if item_type == "commandExecution":
                    synthetic = {
                        "type": "item.completed",
                        "item": {
                            "id": item.get("id"),
                            "type": "command_execution",
                            "aggregated_output": item.get("aggregatedOutput", ""),
                            "exit_code": item.get("exitCode"),
                            "status": (item.get("status") or "").lower(),
                            "error": item.get("error"),
                        },
                    }
                    msg = parser.parse_line(json.dumps(synthetic))
                    if msg:
                        return await handle_stream_message(msg)
                return False

            if method == "turn/completed":
                turn = params.get("turn", {})
                status = str(turn.get("status", "")).lower()
                if status in {"failed", "interrupted"}:
                    turn_error = turn.get("error", {})
                    error_text = (
                        turn_error.get("message", "Codex turn failed")
                        if isinstance(turn_error, dict)
                        else str(turn_error or "Codex turn failed")
                    )
                    msg = parser.parse_line(
                        json.dumps({"type": "turn.failed", "error": {"message": error_text}})
                    )
                else:
                    msg = parser.parse_line(
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "duration_ms": int((time.monotonic() - started_at) * 1000),
                            }
                        )
                    )
                if msg:
                    return await handle_stream_message(msg)
                return True

            if method == "error":
                msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "error",
                            "error": params.get("message", "Codex app-server error"),
                        }
                    )
                )
                if msg:
                    return await handle_stream_message(msg)
                return True

            return False

        async def handle_server_request(request: dict) -> None:
            method = request.get("method", "")
            request_id = request.get("id")
            params = request.get("params", {})

            if request_id is None:
                return

            if method == "item/tool/requestUserInput":
                item_id = str(params.get("itemId", f"request_{request_id}"))
                questions = params.get("questions", [])

                request_msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "request_user_input",
                            "call_id": item_id,
                            "questions": questions,
                        }
                    )
                )
                if request_msg:
                    await handle_stream_message(request_msg)

                response_payload = None
                if on_user_input_request:
                    response_payload = await on_user_input_request(
                        item_id,
                        {"questions": questions},
                    )
                if not isinstance(response_payload, dict):
                    response_payload = self._empty_user_input_response(questions)

                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response_payload,
                    }
                )

                tool_result_msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "tool_result",
                            "tool_use_id": item_id,
                            "content": "User input received",
                            "is_error": False,
                        }
                    )
                )
                if tool_result_msg:
                    await handle_stream_message(tool_result_msg)
                return

            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"decision": "decline"},
                    }
                )
                return

            if method == "skill/requestApproval":
                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"decision": "decline"},
                    }
                )
                return

            if method in {"execCommandApproval", "applyPatchApproval"}:
                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"decision": "denied"},
                    }
                )
                return

            await send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported app-server request method: {method}",
                    },
                }
            )

        async def process_rpc_message(rpc: dict) -> bool:
            response_id = rpc.get("id")
            if response_id is not None and ("result" in rpc or "error" in rpc):
                response_cache[str(response_id)] = rpc
                return False

            method = rpc.get("method")
            if not method:
                return False

            if response_id is not None and "params" in rpc:
                await handle_server_request(rpc)
                return False

            if method.startswith("codex/event/"):
                return False

            return await handle_notification(method, rpc.get("params", {}))

        async def read_rpc_line() -> dict:
            if process.stdout is None:
                raise RuntimeError("app-server stdout is unavailable")
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the stream unexpectedly")
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                return {}
            try:
                return json.loads(line_str)
            except json.JSONDecodeError as e:
                logger.warning(f"{log_prefix}Failed to parse app-server JSON line: {e}")
                return {}

        async def await_response(request_id: int) -> dict:
            while True:
                cache_key = str(request_id)
                if cache_key in response_cache:
                    return response_cache.pop(cache_key)
                rpc = await read_rpc_line()
                if not rpc:
                    continue
                await process_rpc_message(rpc)

        try:
            init_req_id = await send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "slack-claude-code",
                        "version": "1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            init_resp = await await_response(init_req_id)
            if init_resp.get("error"):
                raise RuntimeError(f"initialize failed: {init_resp['error']}")

            thread_params: dict = {
                "cwd": working_directory,
                "approvalPolicy": approval,
                "sandbox": sandbox,
            }
            if base_model:
                thread_params["model"] = base_model

            if resume_session_id:
                thread_method = "thread/resume"
                thread_params["threadId"] = resume_session_id
                logger.info(f"{log_prefix}Resuming session via app-server: {resume_session_id}")
            else:
                thread_method = "thread/start"

            thread_req_id = await send_request(thread_method, thread_params)
            thread_resp = await await_response(thread_req_id)
            if thread_resp.get("error"):
                raise RuntimeError(f"{thread_method} failed: {thread_resp['error']}")
            thread = (thread_resp.get("result") or {}).get("thread", {})
            result_session_id = str(thread.get("id") or result_session_id or "")

            turn_params: dict = {
                "threadId": result_session_id,
                "input": [{"type": "text", "text": effective_prompt}],
            }
            if effort:
                turn_params["effort"] = effort

            turn_req_id = await send_request("turn/start", turn_params)
            turn_resp = await await_response(turn_req_id)
            if turn_resp.get("error"):
                raise RuntimeError(f"turn/start failed: {turn_resp['error']}")

            is_final = False
            while not is_final:
                rpc = await read_rpc_line()
                if not rpc:
                    continue
                is_final = await process_rpc_message(rpc)

            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            stderr = await process.stderr.read() if process.stderr else b""
            if stderr:
                stderr_str = stderr.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning(f"{log_prefix}codex app-server stderr: {stderr_str}")

            success = not error_msg
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
        finally:
            async with self._lock:
                self._active_processes.pop(track_id, None)
                self._process_channels.pop(track_id, None)

    @staticmethod
    def _empty_user_input_response(questions: list[dict]) -> dict:
        """Return a schema-compatible empty answer payload for request_user_input."""
        answers: dict[str, dict[str, list[str]]] = {}
        for i, question in enumerate(questions):
            question_id = str(question.get("id", f"q_{i + 1}"))
            answers[question_id] = {"answers": []}
        return {"answers": answers}

    def _build_effective_prompt(self, prompt: str, log_prefix: str) -> str:
        """Apply Codex default instructions preamble, if configured."""
        if not config.CODEX_PREPEND_DEFAULT_INSTRUCTIONS:
            return prompt

        preamble_path = Path(config.CODEX_DEFAULT_INSTRUCTIONS_FILE).expanduser()
        try:
            preamble = preamble_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.debug(f"{log_prefix}No default Codex instructions file at {preamble_path}")
            return prompt
        except Exception as e:
            logger.warning(
                f"{log_prefix}Failed reading Codex instructions file {preamble_path}: {e}"
            )
            return prompt

        if not preamble:
            return prompt
        if prompt:
            return f"{preamble}\n\n{prompt}"
        return preamble

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
