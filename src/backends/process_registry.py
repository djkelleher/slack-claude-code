"""Shared process tracking helpers for backend executors."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrackedProcess:
    """Tracked process metadata used by executor cancellation paths."""

    track_id: str
    process: asyncio.subprocess.Process
    channel_id: Optional[str]
    session_scope: Optional[str]


class ProcessRegistry:
    """Thread-safe process registry shared by backend executors."""

    def __init__(self) -> None:
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.process_channels: dict[str, str] = {}
        self.process_scopes: dict[str, str] = {}
        self.execution_track_ids: dict[str, str] = {}
        self.lock: asyncio.Lock = asyncio.Lock()

    @staticmethod
    def build_track_id(
        execution_id: Optional[str],
        session_id: Optional[str],
        channel_id: Optional[str],
    ) -> str:
        """Build a stable track id for an execution and optional channel."""
        track_id = execution_id or session_id or f"anon-{uuid.uuid4().hex}"
        if channel_id:
            track_id = f"{channel_id}_{track_id}"
        return track_id

    async def register(
        self,
        *,
        track_id: str,
        process: asyncio.subprocess.Process,
        channel_id: Optional[str],
        session_scope: str,
        execution_id: Optional[str],
    ) -> None:
        """Register a process and related lookup maps."""
        async with self.lock:
            self.active_processes[track_id] = process
            if channel_id:
                self.process_channels[track_id] = channel_id
            self.process_scopes[track_id] = session_scope
            if execution_id:
                self.execution_track_ids[execution_id] = track_id

    async def unregister(
        self,
        *,
        track_id: str,
        execution_id: Optional[str] = None,
    ) -> None:
        """Remove a track id and optional execution mapping without returning process."""
        async with self.lock:
            self.active_processes.pop(track_id, None)
            self.process_channels.pop(track_id, None)
            self.process_scopes.pop(track_id, None)
            if execution_id and self.execution_track_ids.get(execution_id) == track_id:
                self.execution_track_ids.pop(execution_id, None)

    async def pop_for_execution(self, execution_id: str) -> Optional[TrackedProcess]:
        """Pop tracked process for direct execution id or mapped track id."""
        async with self.lock:
            process_key = None
            if execution_id in self.active_processes:
                process_key = execution_id
            else:
                mapped_track = self.execution_track_ids.get(execution_id)
                if mapped_track and mapped_track in self.active_processes:
                    process_key = mapped_track
            if process_key is None:
                return None

            process = self.active_processes.pop(process_key)
            channel = self.process_channels.pop(process_key, None)
            scope = self.process_scopes.pop(process_key, None)
            mapped_track_id = self.execution_track_ids.get(execution_id)
            if mapped_track_id == process_key:
                self.execution_track_ids.pop(execution_id, None)
            return TrackedProcess(
                track_id=process_key,
                process=process,
                channel_id=channel,
                session_scope=scope,
            )

    async def scope_for_execution(self, execution_id: str) -> Optional[str]:
        """Get current scope for an execution id without removing process tracking."""
        async with self.lock:
            process_key = None
            if execution_id in self.active_processes:
                process_key = execution_id
            else:
                mapped_track = self.execution_track_ids.get(execution_id)
                if mapped_track and mapped_track in self.active_processes:
                    process_key = mapped_track
            if process_key is None:
                return None
            return self.process_scopes.get(process_key)

    async def pop_for_scope(self, session_scope: str) -> list[TrackedProcess]:
        """Pop all tracked processes for a session scope."""
        async with self.lock:
            track_ids = [
                track_id
                for track_id, scope in self.process_scopes.items()
                if scope == session_scope
            ]
            popped: list[TrackedProcess] = []
            for track_id in track_ids:
                process = self.active_processes.pop(track_id, None)
                if process is None:
                    continue
                channel = self.process_channels.pop(track_id, None)
                self.process_scopes.pop(track_id, None)
                popped.append(
                    TrackedProcess(
                        track_id=track_id,
                        process=process,
                        channel_id=channel,
                        session_scope=session_scope,
                    )
                )
            for execution_id, mapped_track in list(self.execution_track_ids.items()):
                if mapped_track in track_ids:
                    self.execution_track_ids.pop(execution_id, None)
            return popped

    async def pop_for_channel(self, channel_id: str) -> list[TrackedProcess]:
        """Pop all tracked processes for a channel id."""
        async with self.lock:
            track_ids = [
                track_id
                for track_id, tracked_channel in self.process_channels.items()
                if tracked_channel == channel_id
            ]
            popped: list[TrackedProcess] = []
            for track_id in track_ids:
                process = self.active_processes.pop(track_id, None)
                if process is None:
                    continue
                self.process_channels.pop(track_id, None)
                scope = self.process_scopes.pop(track_id, None)
                popped.append(
                    TrackedProcess(
                        track_id=track_id,
                        process=process,
                        channel_id=channel_id,
                        session_scope=scope,
                    )
                )
            for execution_id, mapped_track in list(self.execution_track_ids.items()):
                if mapped_track in track_ids:
                    self.execution_track_ids.pop(execution_id, None)
            return popped

    async def pop_all(self) -> list[TrackedProcess]:
        """Pop all tracked processes."""
        async with self.lock:
            popped = [
                TrackedProcess(
                    track_id=track_id,
                    process=process,
                    channel_id=self.process_channels.get(track_id),
                    session_scope=self.process_scopes.get(track_id),
                )
                for track_id, process in self.active_processes.items()
            ]
            self.active_processes.clear()
            self.process_channels.clear()
            self.process_scopes.clear()
            self.execution_track_ids.clear()
            return popped

    async def count_for_scope(self, session_scope: str) -> int:
        """Count tracked processes for a session scope."""
        async with self.lock:
            return sum(
                1
                for tracked_scope in self.process_scopes.values()
                if tracked_scope == session_scope
            )

    async def count_for_channel(self, channel_id: str) -> int:
        """Count tracked processes for a channel."""
        async with self.lock:
            return sum(
                1
                for tracked_channel in self.process_channels.values()
                if tracked_channel == channel_id
            )

    async def count_all(self) -> int:
        """Count all tracked processes."""
        async with self.lock:
            return len(self.active_processes)

    async def scopes_for_channel(self, channel_id: str) -> set[str]:
        """Return all session scopes currently tracked for a channel."""
        async with self.lock:
            return {
                scope
                for track_id, scope in self.process_scopes.items()
                if self.process_channels.get(track_id) == channel_id
            }

    async def active_scopes(self) -> list[str]:
        """Return all tracked scopes."""
        async with self.lock:
            return list(self.process_scopes.values())
