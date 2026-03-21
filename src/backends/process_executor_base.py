"""Shared process-tracking lifecycle for subprocess-backed executors."""

import asyncio
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.backends.process_registry import ProcessRegistry
from src.backends.process_termination import terminate_processes
from src.utils.execution_scope import build_session_scope
from src.utils.process_utils import terminate_process_safely


@dataclass(frozen=True)
class ProcessTrackingContext:
    """Process-tracking identifiers for a single executor run."""

    track_id: str
    session_scope: str


class ProcessExecutorBase:
    """Shared process-registry lifecycle used by backend executors."""

    DEFAULT_MAX_RECURSION_DEPTH = 3
    DEFAULT_STREAM_LIMIT_BYTES = 200 * 1024 * 1024

    def __init__(self) -> None:
        self._registry = ProcessRegistry()
        self._lock = self._registry.lock

    @staticmethod
    def create_tracking_context(
        execution_id: Optional[str],
        session_id: Optional[str],
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> ProcessTrackingContext:
        """Create stable tracking identifiers for a process execution."""
        track_id = ProcessRegistry.build_track_id(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
        )
        session_scope = build_session_scope(channel_id or "", thread_ts)
        return ProcessTrackingContext(track_id=track_id, session_scope=session_scope)

    @staticmethod
    def build_log_prefix(db_session_id: Optional[int]) -> str:
        """Build a consistent session-aware logging prefix."""
        return f"[S:{db_session_id}] " if db_session_id else ""

    @classmethod
    def validate_retry_depth(
        cls,
        recursion_depth: int,
        log_prefix: str,
        max_depth: Optional[int] = None,
    ) -> Optional[str]:
        """Return a user-facing error when recursion depth exceeds limits."""
        effective_max_depth = max_depth or cls.DEFAULT_MAX_RECURSION_DEPTH
        if recursion_depth < effective_max_depth:
            return None
        logger.error(
            f"{log_prefix}Max recursion depth ({effective_max_depth}) reached, aborting"
        )
        return f"Max retry depth ({effective_max_depth}) exceeded"

    async def start_subprocess(
        self,
        *,
        cmd: list[str],
        working_directory: str,
        process_label: str,
        log_prefix: str,
        include_stdin: bool = False,
        limit: Optional[int] = None,
    ) -> tuple[Optional[asyncio.subprocess.Process], Optional[str]]:
        """Start a subprocess with shared stdout/stderr/limit defaults."""
        popen_kwargs: dict[str, object] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": working_directory,
            "limit": limit if limit is not None else self.DEFAULT_STREAM_LIMIT_BYTES,
        }
        if include_stdin:
            popen_kwargs["stdin"] = asyncio.subprocess.PIPE

        try:
            process = await asyncio.create_subprocess_exec(*cmd, **popen_kwargs)
            return process, None
        except Exception as e:
            label = process_label.strip() or "subprocess"
            logger.error(f"{log_prefix}Failed to start {label}: {e}")
            return None, f"Failed to start {label}: {e}"

    async def register_process(
        self,
        *,
        context: ProcessTrackingContext,
        process: asyncio.subprocess.Process,
        channel_id: Optional[str],
        execution_id: Optional[str],
    ) -> None:
        """Register a process in shared cancellation lookups."""
        await self._registry.register(
            track_id=context.track_id,
            process=process,
            channel_id=channel_id,
            session_scope=context.session_scope,
            execution_id=execution_id,
        )

    async def unregister_process(
        self,
        *,
        context: ProcessTrackingContext,
        execution_id: Optional[str],
    ) -> None:
        """Unregister a process from shared cancellation lookups."""
        await self._registry.unregister(
            track_id=context.track_id,
            execution_id=execution_id,
        )

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        tracked = await self._registry.pop_for_execution(execution_id)
        if not tracked:
            return False
        await terminate_process_safely(tracked.process)
        return True

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active executions for a channel/thread session scope."""
        tracked = await self._registry.pop_for_scope(session_scope)
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel."""
        tracked = await self._registry.pop_for_channel(channel_id)
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        tracked = await self._registry.pop_all()
        await terminate_processes(entry.process for entry in tracked)
        return len(tracked)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()

    async def has_active_execution(self, session_scope: str) -> bool:
        """Return True when at least one execution is active for the session scope."""
        return await self._registry.count_for_scope(session_scope) > 0
