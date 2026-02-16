"""High-level PTY executor for Codex CLI.

Provides the same interface as SubprocessExecutor but uses persistent PTY sessions.
"""

from typing import Awaitable, Callable, Optional

from loguru import logger

from src.pty.pool import PTYSessionPool
from src.pty.types import PTYSessionConfig, SessionState

from .streaming import StreamMessage
from .subprocess_executor import ExecutionResult


class PTYExecutor:
    """Execute Codex CLI via persistent PTY sessions.

    Provides the same interface as SubprocessExecutor but keeps processes
    alive between executions for faster response times.
    """

    def __init__(
        self,
        max_sessions: int = 10,
        idle_timeout_minutes: int = 30,
        cleanup_interval_seconds: int = 60,
    ) -> None:
        """Initialize the PTY executor.

        Args:
            max_sessions: Maximum concurrent PTY sessions
            idle_timeout_minutes: Minutes before idle sessions are cleaned up
            cleanup_interval_seconds: How often to run cleanup
        """
        PTYSessionPool.max_sessions = max_sessions
        PTYSessionPool.idle_timeout_seconds = idle_timeout_minutes * 60
        PTYSessionPool.cleanup_interval_seconds = cleanup_interval_seconds

        self._cleanup_started = False

    async def start_cleanup_loop(self) -> None:
        """Start the background cleanup loop."""
        if not self._cleanup_started:
            await PTYSessionPool.start_cleanup_loop()
            self._cleanup_started = True

    async def execute(
        self,
        prompt: str,
        channel_id: str,
        thread_ts: Optional[str] = None,
        working_directory: str = "~",
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 216000.0,  # 60 hours
    ) -> ExecutionResult:
        """Execute a prompt via PTY session.

        Args:
            prompt: The prompt to send to Codex
            channel_id: Slack channel ID for session management
            thread_ts: Thread timestamp for thread-scoped sessions
            working_directory: Directory to run Codex in
            on_chunk: Async callback for each streamed message
            sandbox_mode: Sandbox mode (read-only, workspace-write, danger-full-access)
            approval_mode: Approval mode (untrusted, on-failure, on-request, never)
            model: Model to use
            timeout: Maximum time to wait for response

        Returns:
            ExecutionResult with the command output
        """
        # Build session config
        session_config = PTYSessionConfig(
            working_directory=working_directory,
            sandbox_mode=sandbox_mode or "workspace-write",
            approval_mode=approval_mode or "on-request",
            model=model,
        )

        try:
            result = await PTYSessionPool.send_to_session(
                channel_id=channel_id,
                thread_ts=thread_ts,
                prompt=prompt,
                config=session_config,
                on_chunk=on_chunk,
                timeout=timeout,
            )
            return result

        except Exception as e:
            logger.error(f"PTY execution error: {e}")
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
            )

    async def cancel(self, channel_id: str, thread_ts: Optional[str] = None) -> bool:
        """Cancel/interrupt an active execution.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp

        Returns:
            True if interrupt was sent
        """
        return await PTYSessionPool.interrupt_session(channel_id, thread_ts)

    async def stop_session(self, channel_id: str, thread_ts: Optional[str] = None) -> bool:
        """Stop and remove a PTY session.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp

        Returns:
            True if session was found and stopped
        """
        return await PTYSessionPool.remove(channel_id, thread_ts)

    def get_session_info(
        self, channel_id: Optional[str] = None, thread_ts: Optional[str] = None
    ) -> Optional[dict] | list[dict]:
        """Get info about PTY session(s).

        Args:
            channel_id: Optional channel ID
            thread_ts: Optional thread timestamp

        Returns:
            Session info dict or list of dicts
        """
        return PTYSessionPool.get_session_info(channel_id, thread_ts)

    def session_count(self) -> int:
        """Get number of active PTY sessions."""
        return PTYSessionPool.count()

    async def shutdown(self) -> None:
        """Shutdown and cleanup all PTY sessions."""
        await PTYSessionPool.cleanup_all()
        logger.info("PTY executor shutdown complete")
