"""Codex CLI executor using non-interactive JSONL output."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.backends.execution_result import BackendExecutionResult
from src.backends.process_executor_base import ProcessExecutorBase
from src.backends.stream_accumulator import StreamAccumulator
from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config, parse_model_effort
from src.utils.process_utils import terminate_process_safely

from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from src.database.repository import DatabaseRepository


READLINE_TIMEOUT_SECONDS = 1800


@dataclass
class ExecutionResult(BackendExecutionResult):
    """Result of a Codex execution."""


@dataclass
class TurnControlResult:
    """Result for steer/interrupt requests against Codex CLI runs."""

    success: bool
    message: str = ""
    error: Optional[str] = None
    turn_id: Optional[str] = None


class SubprocessExecutor(ProcessExecutorBase):
    """Execute Codex via `codex exec --json`."""

    def __init__(
        self,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        super().__init__()
        self._metrics: dict[str, int] = {
            "queue_fallback_attempts": 0,
            "queue_fallback_successes": 0,
            "queue_fallback_failures": 0,
        }
        self._metrics_lock: asyncio.Lock = asyncio.Lock()
        self.db = db

    async def _increment_metric(self, metric_name: str, count: int = 1) -> None:
        """Increment a named runtime metric counter."""
        async with self._metrics_lock:
            if metric_name not in self._metrics:
                self._metrics[metric_name] = 0
            self._metrics[metric_name] += count

    async def record_queue_fallback(self, success: bool) -> None:
        """Record queue fallback outcome from app-level routing."""
        await self._increment_metric("queue_fallback_attempts")
        if success:
            await self._increment_metric("queue_fallback_successes")
            return
        await self._increment_metric("queue_fallback_failures")

    async def get_metrics_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of runtime Codex integration metrics."""
        async with self._metrics_lock:
            counters = dict(self._metrics)
        attempts = counters.get("queue_fallback_attempts", 0)
        counters["active_turns"] = 0
        counters["queue_fallback_success_rate"] = (
            counters.get("queue_fallback_successes", 0) / attempts if attempts > 0 else 0.0
        )
        counters["steer_success_rate"] = 0.0
        counters["interrupt_success_rate"] = 0.0
        return counters

    async def reset_metrics(self) -> None:
        """Reset runtime Codex integration metrics counters."""
        async with self._metrics_lock:
            for key in list(self._metrics.keys()):
                self._metrics[key] = 0

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
        permission_mode: Optional[str] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        _recursion_depth: int = 0,
    ) -> ExecutionResult:
        """Execute a prompt via Codex CLI JSONL mode."""
        del on_user_input_request, on_approval_request

        log_prefix = self.build_log_prefix(db_session_id)
        effective_prompt = self._build_effective_prompt(prompt, log_prefix)

        retry_error = self.validate_retry_depth(_recursion_depth, log_prefix)
        if retry_error:
            return ExecutionResult(success=False, output="", error=retry_error)

        cmd = self._build_exec_command(
            prompt=effective_prompt,
            working_directory=working_directory,
            resume_session_id=resume_session_id,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            model=model,
            log_prefix=log_prefix,
        )

        process, process_start_error = await self.start_subprocess(
            cmd=cmd,
            working_directory=working_directory,
            process_label="Codex CLI",
            log_prefix=log_prefix,
        )
        if not process:
            return ExecutionResult(
                success=False,
                output="",
                error=process_start_error or "Failed to start Codex CLI",
            )

        tracking = self.create_tracking_context(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        await self.register_process(
            context=tracking,
            process=process,
            channel_id=channel_id,
            execution_id=execution_id,
        )

        parser = StreamParser()
        accumulator = StreamAccumulator(join_assistant_chunks=lambda existing, new: existing + new)
        saw_final_result = False

        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=READLINE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        f"{log_prefix}Readline timeout after {READLINE_TIMEOUT_SECONDS}s - "
                        "Codex process may be hung or lost connection"
                    )
                    await terminate_process_safely(process)
                    return ExecutionResult(
                        **accumulator.result_fields(
                            success=False,
                            error=(
                                f"Codex process timed out (no output for {READLINE_TIMEOUT_SECONDS}s). "
                                "The process may have hung or lost connection to the API."
                            ),
                        )
                    )

                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                msg = parser.parse_line(line_str)
                if not msg:
                    continue

                if msg.type == "init":
                    logger.info(f"{log_prefix}Codex session initialized: {msg.session_id}")
                elif msg.type == "assistant" and msg.content:
                    preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                    logger.debug(f"{log_prefix}Codex: {preview}")
                elif msg.type == "error" and msg.content:
                    if self._is_transient_error_message(msg.content):
                        logger.warning(f"{log_prefix}Codex transient error: {msg.content}")
                        continue
                    logger.error(f"{log_prefix}Codex error: {msg.content}")
                elif msg.type == "result":
                    saw_final_result = True
                    logger.info(
                        f"{log_prefix}Codex finished - duration={msg.duration_ms or 'n/a'}ms"
                    )

                accumulator.apply(msg)
                if on_chunk:
                    await on_chunk(msg)

            return_code = await process.wait()
            stderr_bytes = await process.stderr.read()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.warning(f"{log_prefix}codex stderr: {stderr_text}")

            resolved_error = accumulator.error_message
            success = return_code == 0 and resolved_error is None

            if not success and resolved_error is None:
                resolved_error = stderr_text or f"Codex exited with status {return_code}"

            if (
                not success
                and resume_session_id
                and self._is_missing_thread_error(resolved_error or "")
            ):
                logger.warning(
                    f"{log_prefix}Session {resume_session_id} not found, retrying without resume "
                    f"(depth={_recursion_depth + 1})"
                )
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=permission_mode,
                    sandbox_mode=sandbox_mode,
                    approval_mode=approval_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                )

            if return_code == 0 and not saw_final_result and resolved_error:
                success = False

            return ExecutionResult(
                **accumulator.result_fields(
                    success=success,
                    error=resolved_error,
                    was_cancelled=return_code == -15,
                )
            )
        except asyncio.CancelledError:
            await terminate_process_safely(process)
            raise
        except Exception as e:
            await terminate_process_safely(process)
            logger.error(f"{log_prefix}Error during Codex CLI execution: {e}")
            return ExecutionResult(**accumulator.result_fields(success=False, error=str(e)))
        finally:
            await self.unregister_process(context=tracking, execution_id=execution_id)

    def _build_exec_command(
        self,
        *,
        prompt: str,
        working_directory: str,
        resume_session_id: Optional[str],
        sandbox_mode: Optional[str],
        approval_mode: Optional[str],
        model: Optional[str],
        log_prefix: str,
    ) -> list[str]:
        """Build a `codex exec` command line."""
        sandbox = self._resolve_sandbox_mode(sandbox_mode, log_prefix)
        approval = self._resolve_approval_mode(approval_mode, log_prefix)
        base_model, effort = parse_model_effort(model) if model else (None, None)

        cmd = ["codex"]
        if approval:
            cmd.extend(["-a", approval])
        if sandbox:
            cmd.extend(["-s", sandbox])
        cmd.extend(["-C", working_directory])
        if base_model:
            cmd.extend(["-m", base_model])
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])

        cmd.extend(["exec"])
        if resume_session_id:
            cmd.extend(["resume", resume_session_id])
            logger.info(f"{log_prefix}Resuming Codex session {resume_session_id}")
        cmd.extend(["--json", "--skip-git-repo-check", prompt])

        preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"{log_prefix}Executing via Codex CLI: {' '.join(cmd[:-1])} '{preview}'")
        return cmd

    @staticmethod
    def _resolve_sandbox_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated sandbox mode."""
        resolved = mode or config.CODEX_SANDBOX_MODE
        if resolved not in config.VALID_SANDBOX_MODES:
            logger.warning(
                f"{log_prefix}Invalid sandbox mode: {resolved}, using {config.CODEX_SANDBOX_MODE}"
            )
            return config.CODEX_SANDBOX_MODE
        return resolved

    @staticmethod
    def _resolve_approval_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated approval mode."""
        normalized = normalize_codex_approval_mode(mode or config.CODEX_APPROVAL_MODE)
        if normalized not in config.VALID_APPROVAL_MODES:
            logger.warning(
                f"{log_prefix}Invalid approval mode: {normalized}, using {config.CODEX_APPROVAL_MODE}"
            )
            return normalize_codex_approval_mode(config.CODEX_APPROVAL_MODE)
        return normalized

    @staticmethod
    def _is_missing_thread_error(error: Any) -> bool:
        """Return True when a resume error indicates session missing."""
        error_text = str(error).lower()
        markers = (
            "thread not found",
            "session not found",
            "no conversation found",
            "unknown thread",
        )
        return any(marker in error_text for marker in markers)

    @staticmethod
    def _is_transient_error_message(message: str) -> bool:
        """Return True when a streamed error line is informational and retryable."""
        lowered = (message or "").strip().lower()
        return lowered.startswith("reconnecting...")

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

    async def has_active_turn(self, session_scope: str) -> bool:
        """Return False because `codex exec` runs are not steerable."""
        del session_scope
        return False

    async def get_active_turn(self, session_scope: str) -> Optional[dict]:
        """Return no active turn metadata for CLI-based executions."""
        del session_scope
        return None

    async def steer_active_turn(
        self,
        session_scope: str,
        text: str,
        timeout: float = 10.0,
    ) -> TurnControlResult:
        """Steering is unsupported in CLI execution mode."""
        del session_scope, text, timeout
        return TurnControlResult(
            success=False,
            error="Active-turn steering is not supported with `codex exec`.",
        )

    async def interrupt_active_turn(
        self,
        session_scope: str,
        timeout: float = 10.0,
    ) -> TurnControlResult:
        """Interrupting active turns is unsupported in CLI execution mode."""
        del session_scope, timeout
        return TurnControlResult(
            success=False,
            error="Active-turn interruption is not supported with `codex exec`.",
        )

    async def thread_read(
        self,
        thread_id: str,
        working_directory: str,
        include_turns: bool = False,
    ) -> dict:
        """Thread inspection is not available in CLI mode."""
        del thread_id, working_directory, include_turns
        raise RuntimeError("Codex CLI mode does not support thread inspection.")

    async def thread_fork(self, thread_id: str, working_directory: str) -> dict:
        """Thread forking is intentionally disabled in CLI mode."""
        del thread_id, working_directory
        raise RuntimeError("Codex CLI mode does not support thread forking.")

    async def review_start(self, thread_id: str, target: dict, working_directory: str) -> dict:
        """Run `codex exec review --json` and return a minimal summary payload."""
        del thread_id

        prompt = ""
        cmd = [
            "codex",
            "-C",
            working_directory,
            "exec",
            "review",
            "--json",
            "--skip-git-repo-check",
        ]
        if target.get("type") == "uncommittedChanges":
            cmd.append("--uncommitted")
        elif target.get("type") == "custom":
            prompt = str(target.get("instructions") or "")
        if prompt:
            cmd.append(prompt)

        process, process_start_error = await self.start_subprocess(
            cmd=cmd,
            working_directory=working_directory,
            process_label="Codex review",
            log_prefix="",
        )
        if not process:
            raise RuntimeError(process_start_error or "Failed to start Codex review")

        parser = StreamParser()
        accumulator = StreamAccumulator(join_assistant_chunks=lambda existing, new: existing + new)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            msg = parser.parse_line(line.decode("utf-8", errors="replace").strip())
            if not msg:
                continue
            if msg.type == "error" and self._is_transient_error_message(msg.content):
                continue
            accumulator.apply(msg)

        return_code = await process.wait()
        stderr_bytes = await process.stderr.read()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if return_code != 0:
            raise RuntimeError(accumulator.error_message or stderr_text or "Codex review failed")

        return {
            "reviewThreadId": None,
            "turn": {
                "id": accumulator.session_id or "codex-cli-review",
                "status": "completed",
            },
            "output": accumulator.output,
        }

    async def model_list(self, working_directory: str) -> dict:
        """Return empty model metadata in CLI mode."""
        del working_directory
        return {"data": []}

    async def account_read(self, working_directory: str) -> dict:
        """Return unavailable account metadata in CLI mode."""
        del working_directory
        return {"account": None}

    async def account_rate_limits_read(self, working_directory: str) -> dict:
        """Return unavailable rate-limit metadata in CLI mode."""
        del working_directory
        return {}

    async def config_read(self, working_directory: str) -> dict:
        """Return minimal config metadata available from app settings."""
        del working_directory
        model = config.DEFAULT_MODEL or ""
        base_model, effort = parse_model_effort(model) if model else (None, None)
        return {
            "config": {
                "model": base_model or model or None,
                "model_reasoning_effort": effort,
            }
        }

    async def experimental_feature_list(self, working_directory: str) -> dict:
        """Return empty feature metadata in CLI mode."""
        del working_directory
        return {"data": []}

    async def mcp_server_status_list(self, working_directory: str) -> dict:
        """Return empty MCP status metadata in CLI mode."""
        del working_directory
        return {"data": []}
