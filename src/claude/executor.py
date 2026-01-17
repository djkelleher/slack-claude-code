"""Claude Code executor using persistent PTY sessions.

This module provides the ClaudeExecutor class that manages command execution
via persistent PTY sessions, keeping Claude Code running in interactive mode.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

from ..config import config
from ..hooks.registry import HookRegistry, create_context
from ..hooks.types import HookEvent, HookEventType
from ..pty.pool import PTYSessionPool
from ..pty.types import ResponseChunk
from .streaming import StreamMessage

logger = logging.getLogger(__name__)


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
    was_permission_request: bool = False


class ClaudeExecutor:
    """Manages Claude Code execution via persistent PTY sessions.

    Uses PTYSessionPool to maintain long-running Claude Code processes,
    allowing multiple commands to be sent without restarting.
    """

    def __init__(
        self,
        timeout: int = None,
    ) -> None:
        self.timeout = timeout or config.timeouts.execution.command
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Initialize session pool cleanup loop."""
        if not self._initialized:
            await PTYSessionPool.start_cleanup_loop()
            self._initialized = True

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
    ) -> ExecutionResult:
        """Execute a command via persistent PTY session.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Session identifier (typically channel_id)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message

        Returns:
            ExecutionResult with the command output.
        """
        await self._ensure_initialized()

        channel_id = session_id or execution_id or "default"

        start_time = asyncio.get_event_loop().time()

        async def handle_chunk(chunk: ResponseChunk) -> None:
            """Convert ResponseChunk to StreamMessage for callback."""
            if on_chunk:
                msg_type = "assistant"
                if chunk.output_type.value == "tool_use":
                    msg_type = "tool"
                elif chunk.output_type.value == "permission":
                    msg_type = "permission"
                elif chunk.is_final:
                    msg_type = "result"

                msg = StreamMessage(
                    type=msg_type,
                    content=chunk.content,
                    session_id=channel_id,
                    is_final=chunk.is_final,
                    raw={
                        "tool_name": chunk.tool_name,
                        "tool_input": chunk.tool_input,
                        "is_permission_request": chunk.is_permission_request,
                    },
                )
                await on_chunk(msg)

        try:
            response = await PTYSessionPool.send_to_session(
                session_id=channel_id,
                prompt=prompt,
                working_directory=working_directory,
                on_chunk=handle_chunk,
                timeout=self.timeout,
            )

            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Emit RESULT hook
            await HookRegistry.emit(
                HookEvent(
                    event_type=HookEventType.RESULT,
                    context=create_context(
                        session_id=channel_id,
                        working_directory=working_directory,
                    ),
                    data={
                        "success": response.success,
                        "duration_ms": duration_ms,
                        "output_length": len(response.output) if response.output else 0,
                    },
                )
            )

            return ExecutionResult(
                success=response.success,
                output=response.output,
                session_id=channel_id,
                error=response.error,
                duration_ms=duration_ms,
                was_permission_request=response.was_permission_request,
            )

        except Exception as e:
            # Emit ERROR hook
            await HookRegistry.emit(
                HookEvent(
                    event_type=HookEventType.ERROR,
                    context=create_context(
                        session_id=channel_id,
                        working_directory=working_directory,
                    ),
                    data={"error": str(e)},
                )
            )

            return ExecutionResult(
                success=False,
                output="",
                session_id=channel_id,
                error=str(e),
            )

    async def execute_streaming(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> AsyncIterator[StreamMessage]:
        """Execute Claude CLI and yield streaming messages.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Session identifier (typically channel_id)
            execution_id: Unique ID for this execution

        Yields:
            StreamMessage objects as they arrive.
        """
        await self._ensure_initialized()

        channel_id = session_id or execution_id or "default"
        queue: asyncio.Queue[StreamMessage] = asyncio.Queue()
        done = asyncio.Event()

        async def on_chunk(chunk: ResponseChunk) -> None:
            """Queue chunks for streaming."""
            msg_type = "assistant"
            if chunk.output_type.value == "tool_use":
                msg_type = "tool"
            elif chunk.output_type.value == "permission":
                msg_type = "permission"
            elif chunk.is_final:
                msg_type = "result"

            msg = StreamMessage(
                type=msg_type,
                content=chunk.content,
                session_id=channel_id,
                is_final=chunk.is_final,
                raw={
                    "tool_name": chunk.tool_name,
                    "tool_input": chunk.tool_input,
                    "is_permission_request": chunk.is_permission_request,
                },
            )
            await queue.put(msg)

            if chunk.is_final:
                done.set()

        # Start execution in background
        async def run_execution():
            try:
                await PTYSessionPool.send_to_session(
                    session_id=channel_id,
                    prompt=prompt,
                    working_directory=working_directory,
                    on_chunk=on_chunk,
                    timeout=self.timeout,
                )
            except Exception as e:
                await queue.put(
                    StreamMessage(
                        type="error",
                        content=str(e),
                        is_final=True,
                    )
                )
            finally:
                done.set()

        task = asyncio.create_task(run_execution())

        try:
            while not done.is_set() or not queue.empty():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=0.1)
                    yield msg
                    if msg.is_final:
                        break
                except asyncio.TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution by interrupting the session."""
        return await PTYSessionPool.interrupt_session(execution_id)

    async def cancel_all(self) -> int:
        """Cancel all active sessions. Returns count of cancelled."""
        sessions = await PTYSessionPool.list_sessions()
        count = 0
        for session_id in sessions:
            if await PTYSessionPool.interrupt_session(session_id):
                count += 1
        return count

    async def shutdown(self) -> None:
        """Shutdown executor and all sessions."""
        await PTYSessionPool.cleanup_all()

    async def respond_to_approval(
        self,
        session_id: str,
        approved: bool,
    ) -> bool:
        """Respond to a permission request.

        Args:
            session_id: The session with the pending approval
            approved: True to approve, False to deny

        Returns:
            True if response was sent successfully
        """
        return await PTYSessionPool.respond_to_approval(session_id, approved)

    def get_session_info(self) -> list[dict]:
        """Get information about active sessions."""
        return PTYSessionPool.get_session_info()

    async def terminate_session(self, session_id: str) -> bool:
        """Terminate a specific session."""
        return await PTYSessionPool.remove(session_id)
