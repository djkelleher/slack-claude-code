"""Subprocess executor for Google Gemini CLI.

Proof-of-concept executor that spawns the ``gemini`` CLI as a subprocess
and streams its output. This demonstrates how to add a new subprocess-based
backend to the registry system.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.backends.execution_result import BackendExecutionResult
from src.backends.process_executor_base import ProcessExecutorBase
from src.backends.stream_accumulator import StreamAccumulator
from src.gemini.streaming import StreamParser
from src.utils.stream_models import StreamMessage


class SubprocessExecutor(ProcessExecutorBase):
    """Execute prompts via the Gemini CLI subprocess.

    This is a minimal executor for proof-of-concept. It spawns ``gemini``
    with the prompt on stdin and collects output.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        **kwargs: Any,
    ) -> BackendExecutionResult:
        """Execute a prompt using the Gemini CLI.

        Parameters
        ----------
        prompt : str
            The user's prompt text.
        working_directory : str
            Working directory for the subprocess.
        model : Optional[str]
            Model to use (e.g., "gemini-2.5-pro").
        on_chunk : Optional[Callable]
            Callback for streaming output chunks.

        Returns
        -------
        BackendExecutionResult
            Execution result with output text.
        """
        log_prefix = self.build_log_prefix(db_session_id)
        start_time = time.monotonic()

        cmd = ["gemini"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-p", prompt])

        logger.info(f"{log_prefix} Starting Gemini CLI: {' '.join(cmd[:4])}...")

        tracking = self.create_tracking_context(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                limit=self.DEFAULT_STREAM_LIMIT_BYTES,
            )

            self._registry.register(
                track_id=tracking.track_id,
                process=process,
                session_scope=tracking.session_scope,
            )

            parser = StreamParser()
            accumulator = StreamAccumulator()

            if process.stdout:
                async for line_bytes in process.stdout:
                    line = line_bytes.decode("utf-8", errors="replace")
                    msg = parser.parse_line(line)
                    if msg:
                        accumulator.apply(msg)
                        if on_chunk:
                            await on_chunk(msg)

            await process.wait()

            stderr_output = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr_output = stderr_bytes.decode("utf-8", errors="replace")

            duration_ms = int((time.monotonic() - start_time) * 1000)
            success = process.returncode == 0

            if not success and stderr_output:
                logger.warning(f"{log_prefix} Gemini CLI stderr: {stderr_output[:500]}")

            result_fields = accumulator.result_fields()
            return BackendExecutionResult(
                success=success,
                output=result_fields.get("output", ""),
                detailed_output=result_fields.get("detailed_output", ""),
                session_id=None,
                error=stderr_output if not success else None,
                duration_ms=duration_ms,
            )

        except FileNotFoundError:
            return BackendExecutionResult(
                success=False,
                output="",
                error="Gemini CLI not found. Install it with: npm install -g @anthropic-ai/gemini",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        except Exception as e:
            logger.error(f"{log_prefix} Gemini execution error: {e}")
            return BackendExecutionResult(
                success=False,
                output="",
                error=str(e),
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
        finally:
            self._registry.unregister(tracking.track_id)
