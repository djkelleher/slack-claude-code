"""Codex app-server executor using subprocess JSON-RPC over stdio."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.codex.approval_bridge import default_approval_payload
from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config, parse_model_effort

from src.claude.streaming import _concat_with_spacing

from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from src.database.repository import DatabaseRepository


@dataclass
class ExecutionResult:
    """Result of a Codex execution."""

    success: bool
    output: str
    detailed_output: str = ""  # Full output with tool use details
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False


class SubprocessExecutor:
    """Execute Codex via `codex app-server` JSON-RPC over stdio."""

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
        on_approval_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        _recursion_depth: int = 0,
    ) -> ExecutionResult:
        """Execute a prompt via Codex app-server.

        Args:
            prompt: The prompt to send to Codex.
            working_directory: Directory to run Codex in.
            session_id: Identifier for this execution (for tracking).
            resume_session_id: Codex thread ID to resume (from previous execution).
            execution_id: Unique ID for this execution (for cancellation).
            on_chunk: Async callback for each streamed message.
            on_user_input_request: Callback for request_user_input prompts.
            on_approval_request: Callback for command/file/skill approval prompts.
            sandbox_mode: Sandbox mode (read-only, workspace-write, danger-full-access).
            approval_mode: Approval mode (untrusted, on-request, never).
            db_session_id: Database session ID for tracking.
            model: Model to use (e.g., "gpt-5.3-codex").
            channel_id: Slack channel ID (for process tracking).
            _recursion_depth: Internal retry depth for resume recovery.

        Returns:
            ExecutionResult with command output.
        """
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        effective_prompt = self._build_effective_prompt(prompt, log_prefix)

        max_recursion_depth = 3
        if _recursion_depth >= max_recursion_depth:
            logger.error(
                f"{log_prefix}Max recursion depth ({max_recursion_depth}) reached, aborting"
            )
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max retry depth ({max_recursion_depth}) exceeded",
            )

        return await self._execute_via_app_server(
            prompt=prompt,
            effective_prompt=effective_prompt,
            working_directory=working_directory,
            session_id=session_id,
            resume_session_id=resume_session_id,
            execution_id=execution_id,
            on_chunk=on_chunk,
            on_user_input_request=on_user_input_request,
            on_approval_request=on_approval_request,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            db_session_id=db_session_id,
            model=model,
            channel_id=channel_id,
            _recursion_depth=_recursion_depth,
        )

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
        on_approval_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
        sandbox_mode: Optional[str],
        approval_mode: Optional[str],
        db_session_id: Optional[int],
        model: Optional[str],
        channel_id: Optional[str],
        _recursion_depth: int,
    ) -> ExecutionResult:
        """Execute using Codex app-server JSON-RPC flow."""
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        logger.info(f"{log_prefix}Executing via `codex app-server` JSON-RPC flow")

        approval = self._resolve_approval_mode(approval_mode, log_prefix)
        sandbox = self._resolve_sandbox_mode(sandbox_mode, log_prefix)

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
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to start codex app-server: {e}",
            )

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

        async def emit_assistant_delta(delta: str) -> bool:
            if not delta:
                return False
            msg = parser.parse_line(
                json.dumps(
                    {
                        "type": "assistant",
                        "content": delta,
                    }
                )
            )
            if msg:
                return await handle_stream_message(msg)
            return False

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

            if method in {"item/agentMessage/delta", "item/plan/delta"}:
                return await emit_assistant_delta(str(params.get("delta", "")))

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
                "skill/requestApproval",
                "execCommandApproval",
                "applyPatchApproval",
            }:
                response_payload = None
                if on_approval_request:
                    response_payload = await on_approval_request(method, params)

                if not self._is_valid_approval_response(method, response_payload):
                    response_payload = default_approval_payload(method, approval)

                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response_payload,
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

            thread_params: dict[str, Any] = {
                "cwd": working_directory,
                "approvalPolicy": approval,
                "sandbox": sandbox,
            }
            if base_model:
                thread_params["model"] = base_model

            thread_method = "thread/start"
            if resume_session_id:
                thread_method = "thread/resume"
                thread_params["threadId"] = resume_session_id
                logger.info(f"{log_prefix}Resuming session via app-server: {resume_session_id}")

            thread_req_id = await send_request(thread_method, thread_params)
            thread_resp = await await_response(thread_req_id)

            if (
                thread_resp.get("error")
                and resume_session_id
                and self._is_missing_thread_error(thread_resp["error"])
            ):
                logger.info(
                    f"{log_prefix}Session {resume_session_id} not found, "
                    "retrying with a new thread"
                )
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    on_user_input_request=on_user_input_request,
                    on_approval_request=on_approval_request,
                    sandbox_mode=sandbox,
                    approval_mode=approval,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    _recursion_depth=_recursion_depth + 1,
                )

            if thread_resp.get("error"):
                raise RuntimeError(f"{thread_method} failed: {thread_resp['error']}")

            thread = (thread_resp.get("result") or {}).get("thread", {})
            result_session_id = str(thread.get("id") or result_session_id or "")

            turn_params: dict[str, Any] = {
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
        except Exception as e:
            logger.error(f"{log_prefix}Error during app-server execution: {e}")
            if process.returncode is None:
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

    @staticmethod
    def _resolve_sandbox_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated sandbox mode."""
        resolved = mode or config.CODEX_SANDBOX_MODE
        if resolved not in config.VALID_SANDBOX_MODES:
            logger.warning(
                f"{log_prefix}Invalid sandbox mode: {resolved}, "
                f"using {config.CODEX_SANDBOX_MODE}"
            )
            return config.CODEX_SANDBOX_MODE
        logger.info(f"{log_prefix}Using sandbox mode: {resolved}")
        return resolved

    @staticmethod
    def _resolve_approval_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated approval mode."""
        normalized = normalize_codex_approval_mode(mode or config.CODEX_APPROVAL_MODE)
        if normalized not in config.VALID_APPROVAL_MODES:
            logger.warning(
                f"{log_prefix}Invalid approval mode: {normalized}, "
                f"using {config.CODEX_APPROVAL_MODE}"
            )
            return normalize_codex_approval_mode(config.CODEX_APPROVAL_MODE)
        logger.info(f"{log_prefix}Using approval mode: {normalized}")
        return normalized

    @staticmethod
    def _is_missing_thread_error(error: Any) -> bool:
        """Return True when an app-server resume error indicates thread/session missing."""
        error_text = str(error).lower()
        markers = (
            "thread not found",
            "session not found",
            "no conversation found",
            "unknown thread",
        )
        return any(marker in error_text for marker in markers)

    @staticmethod
    def _is_valid_approval_response(method: str, payload: Any) -> bool:
        """Validate approval payload shape for known request methods."""
        if not isinstance(payload, dict):
            return False
        decision = payload.get("decision")
        if decision is None:
            return False

        normalized_method = (method or "").strip()

        if normalized_method == "skill/requestApproval":
            return decision in {"approve", "decline"}

        if normalized_method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            simple_decisions = {"accept", "acceptForSession", "decline", "cancel"}
            if isinstance(decision, str):
                return decision in simple_decisions
            if isinstance(decision, dict):
                # Allow advanced app-server decision payloads.
                return bool(decision)
            return False

        if normalized_method in {"execCommandApproval", "applyPatchApproval"}:
            return decision in {"approved", "denied"}

        return True

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
            channel_id: The Slack channel ID to cancel executions for.

        Returns:
            Number of processes cancelled.
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
