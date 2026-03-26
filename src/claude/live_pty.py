"""Live PTY session manager for interactive Claude execution."""

import asyncio
import errno
import os
import re
import signal
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Awaitable, Callable, Optional

from loguru import logger

from src.config import parse_claude_model_effort
from src.utils.stream_models import StreamMessage

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PROMPT_MARKER_RE = re.compile(r"(?:^|\n)[^\S\n]{0,4}(?:>|❯) {1,2}$")
TRUNCATION_NOTICE = "_Output truncated to recent PTY buffer window._"


@dataclass
class PtySteerResult:
    """Result for live-input steering into an active PTY turn."""

    success: bool
    error: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass
class PtyTurnResult:
    """Result for one PTY turn execution."""

    success: bool
    output: str
    session_id: Optional[str]
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    detailed_output: str = ""
    was_cancelled: bool = False


@dataclass
class _LivePtySession:
    """In-memory state for one scope-bound Claude PTY process."""

    scope: str
    process: asyncio.subprocess.Process
    master_fd: int
    session_id: str
    working_directory: str
    model: Optional[str]
    permission_mode: str
    added_dirs: tuple[str, ...]
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_turn_id: Optional[str] = None
    cancel_requested: bool = False
    turn_count: int = 0
    last_activity: float = field(default_factory=monotonic)

    def is_running(self) -> bool:
        """Return True when the underlying process is still alive."""
        return self.process.returncode is None

    def is_active(self) -> bool:
        """Return True when a turn is currently in-flight."""
        return self.active_turn_id is not None


class ClaudeLivePtyManager:
    """Manage one interactive Claude PTY process per Slack session scope."""

    def __init__(self) -> None:
        self._sessions: dict[str, _LivePtySession] = {}
        self._sessions_lock = asyncio.Lock()
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._janitor_task: Optional[asyncio.Task[None]] = None
        self._janitor_idle_timeout_seconds: int = 0
        self._janitor_interval_seconds: float = 30.0

    async def has_active_run(self, session_scope: str) -> bool:
        """Return True when the scope currently has an active PTY turn."""
        async with self._sessions_lock:
            session = self._sessions.get(session_scope)
            return bool(session and session.is_running() and session.is_active())

    async def steer_active_run(self, session_scope: str, text: str) -> PtySteerResult:
        """Inject user input into an active PTY turn."""
        async with self._sessions_lock:
            session = self._sessions.get(session_scope)
            if not session or not session.is_running() or not session.is_active():
                return PtySteerResult(success=False, error="No active Claude PTY turn")

        try:
            await self._write_text(session, f"{text.rstrip()}\n")
            return PtySteerResult(success=True, turn_id=session.active_turn_id)
        except Exception as e:
            logger.warning(f"Failed steering active PTY turn for {session_scope}: {e}")
            return PtySteerResult(
                success=False, error=str(e), turn_id=session.active_turn_id
            )

    async def execute_turn(
        self,
        *,
        session_scope: str,
        prompt: str,
        working_directory: str,
        resume_session_id: Optional[str],
        model: Optional[str],
        permission_mode: str,
        added_dirs: list[str],
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]],
        turn_id: Optional[str],
        turn_timeout_seconds: int,
        read_timeout_seconds: float,
        settle_seconds: float,
        cancel_settle_seconds: float,
        promptless_idle_fallback_seconds: float,
        max_output_chars: int,
        idle_session_timeout_seconds: int,
        log_prefix: str,
    ) -> PtyTurnResult:
        """Run one prompt turn in an interactive PTY session."""
        await self.close_idle_sessions(idle_session_timeout_seconds)
        session = await self._ensure_session(
            session_scope=session_scope,
            working_directory=working_directory,
            resume_session_id=resume_session_id,
            model=model,
            permission_mode=permission_mode,
            added_dirs=added_dirs,
            log_prefix=log_prefix,
        )

        async with session.turn_lock:
            run_id = turn_id or f"pty-{uuid.uuid4().hex[:8]}"
            session.active_turn_id = run_id
            session.cancel_requested = False
            session.last_activity = monotonic()
            started_at = monotonic()
            output_buffer = ""
            echo_suppressed = False
            got_output = False
            last_output_at = started_at
            was_cancelled = False
            cancel_seen_at: Optional[float] = None
            prompt_seen = False
            tail_window = ""
            output_truncated = False

            try:
                await self._drain_pending_output(session, timeout_seconds=0.15)
                await self._write_text(session, f"{prompt.rstrip()}\n")

                while True:
                    now = monotonic()
                    if now - started_at > float(turn_timeout_seconds):
                        return PtyTurnResult(
                            success=False,
                            output="",
                            error=(
                                "Live PTY turn timed out before completion "
                                f"({turn_timeout_seconds}s)."
                            ),
                            session_id=session.session_id,
                            duration_ms=int((now - started_at) * 1000),
                        )

                    chunk = await self._read_chunk(
                        session, timeout_seconds=read_timeout_seconds
                    )
                    now = monotonic()
                    if chunk is None:
                        if session.cancel_requested:
                            if cancel_seen_at is None:
                                cancel_seen_at = now
                            if (now - cancel_seen_at) >= cancel_settle_seconds:
                                was_cancelled = True
                                break
                            continue
                        if (
                            prompt_seen
                            and got_output
                            and (now - last_output_at) >= settle_seconds
                        ):
                            break
                        if (
                            got_output
                            and not prompt_seen
                            and (now - last_output_at)
                            >= promptless_idle_fallback_seconds
                        ):
                            break
                        continue
                    if chunk == "":
                        if got_output:
                            break
                        return PtyTurnResult(
                            success=False,
                            output="",
                            error="Claude PTY session closed unexpectedly.",
                            session_id=session.session_id,
                            duration_ms=int((now - started_at) * 1000),
                        )

                    session.last_activity = now
                    got_output = True
                    last_output_at = now

                    normalized = self._normalize_terminal_chunk(chunk)
                    if not normalized:
                        continue
                    tail_window = (tail_window + normalized)[-512:]
                    if self._tail_has_prompt_marker(tail_window):
                        prompt_seen = True

                    if not echo_suppressed:
                        normalized = self._strip_first_prompt_echo(normalized, prompt)
                        echo_suppressed = True
                    if not normalized:
                        continue

                    output_buffer += normalized
                    if max_output_chars > 0 and len(output_buffer) > max_output_chars:
                        output_buffer = output_buffer[-max_output_chars:]
                        output_truncated = True
                    if on_chunk:
                        await on_chunk(
                            StreamMessage(type="assistant", content=normalized)
                        )

                finished_at = monotonic()
                raw_output = output_buffer
                output = self._finalize_output_text(raw_output)
                if output_truncated:
                    output = (
                        f"{TRUNCATION_NOTICE}\n\n{output}"
                        if output
                        else TRUNCATION_NOTICE
                    )
                session.turn_count += 1

                if was_cancelled:
                    return PtyTurnResult(
                        success=False,
                        output=output or "_Cancelled._",
                        error="Cancelled",
                        session_id=session.session_id,
                        duration_ms=int((finished_at - started_at) * 1000),
                        detailed_output=output or "_Cancelled._",
                        was_cancelled=True,
                    )

                return PtyTurnResult(
                    success=True,
                    output=output,
                    session_id=session.session_id,
                    duration_ms=int((finished_at - started_at) * 1000),
                    detailed_output=output,
                )
            finally:
                session.active_turn_id = None
                session.cancel_requested = False
                session.last_activity = monotonic()

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Interrupt the active turn for one scope."""
        async with self._sessions_lock:
            session = self._sessions.get(session_scope)
            if not session or not session.is_running() or not session.is_active():
                return 0
            session.cancel_requested = True
        await self._send_ctrl_c(session)
        return 1

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Interrupt all active turns for channel-scoped sessions."""
        prefix = f"{channel_id}:"
        async with self._sessions_lock:
            sessions = [
                s
                for scope, s in self._sessions.items()
                if scope.startswith(prefix) and s.is_running() and s.is_active()
            ]
            for session in sessions:
                session.cancel_requested = True
        for session in sessions:
            await self._send_ctrl_c(session)
        return len(sessions)

    async def cancel_all(self) -> int:
        """Interrupt all active PTY turns."""
        async with self._sessions_lock:
            sessions = [
                s for s in self._sessions.values() if s.is_running() and s.is_active()
            ]
            for session in sessions:
                session.cancel_requested = True
        for session in sessions:
            await self._send_ctrl_c(session)
        return len(sessions)

    async def close_idle_sessions(self, idle_timeout_seconds: int) -> None:
        """Close inactive sessions that exceeded idle timeout."""
        now = monotonic()
        to_close: list[_LivePtySession] = []
        async with self._sessions_lock:
            for scope, session in list(self._sessions.items()):
                if not session.is_running():
                    self._sessions.pop(scope, None)
                    to_close.append(session)
                    continue
                if session.is_active() or session.turn_lock.locked():
                    continue
                if (now - session.last_activity) >= float(idle_timeout_seconds):
                    self._sessions.pop(scope, None)
                    to_close.append(session)
        for session in to_close:
            await self._terminate_session(session)

    async def shutdown(self) -> None:
        """Terminate all PTY processes and clear session registry."""
        await self.stop_idle_janitor()
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._terminate_session(session)

    async def ensure_idle_janitor(
        self,
        *,
        idle_timeout_seconds: int,
        interval_seconds: float,
    ) -> None:
        """Start or reconfigure the background idle-session janitor."""
        self._janitor_idle_timeout_seconds = max(1, int(idle_timeout_seconds))
        self._janitor_interval_seconds = max(0.2, float(interval_seconds))
        if self._janitor_task and not self._janitor_task.done():
            return
        self._janitor_task = asyncio.create_task(self._idle_janitor_loop())

    async def stop_idle_janitor(self) -> None:
        """Stop the background idle-session janitor task."""
        task = self._janitor_task
        self._janitor_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _idle_janitor_loop(self) -> None:
        """Periodically close stale idle PTY sessions."""
        try:
            while True:
                await asyncio.sleep(self._janitor_interval_seconds)
                await self.close_idle_sessions(self._janitor_idle_timeout_seconds)
        except asyncio.CancelledError:
            return

    async def _ensure_session(
        self,
        *,
        session_scope: str,
        working_directory: str,
        resume_session_id: Optional[str],
        model: Optional[str],
        permission_mode: str,
        added_dirs: list[str],
        log_prefix: str,
    ) -> _LivePtySession:
        """Return existing compatible session or create a new process."""
        normalized_dirs = tuple(d for d in added_dirs if d)
        requested_resume = (
            resume_session_id if self._is_uuid(resume_session_id) else None
        )
        scope_lock = await self._get_scope_lock(session_scope)
        sessions_to_close: list[_LivePtySession] = []

        async with scope_lock:
            while True:
                async with self._sessions_lock:
                    existing = self._sessions.get(session_scope)

                    if existing and not existing.is_running():
                        self._sessions.pop(session_scope, None)
                        sessions_to_close.append(existing)
                        existing = None

                    if existing:
                        reset_requested = (
                            requested_resume is None and existing.turn_count > 0
                        )
                        compatibility_mismatch = (
                            existing.working_directory != working_directory
                            or existing.model != model
                            or existing.permission_mode != permission_mode
                            or existing.added_dirs != normalized_dirs
                            or (
                                requested_resume is not None
                                and existing.session_id != requested_resume
                            )
                        )
                        if reset_requested or compatibility_mismatch:
                            self._sessions.pop(session_scope, None)
                            sessions_to_close.append(existing)
                            existing = None

                    if existing:
                        return existing

                while sessions_to_close:
                    stale = sessions_to_close.pop()
                    await self._terminate_session(stale)

                session = await self._spawn_session(
                    session_scope=session_scope,
                    working_directory=working_directory,
                    requested_resume=requested_resume,
                    model=model,
                    permission_mode=permission_mode,
                    normalized_dirs=normalized_dirs,
                    log_prefix=log_prefix,
                )
                async with self._sessions_lock:
                    current = self._sessions.get(session_scope)
                    if current is None:
                        self._sessions[session_scope] = session
                        break
                await self._terminate_session(session)
                # Another task won insertion race despite scope lock; retry safely.

        await self._drain_pending_output(session, timeout_seconds=0.5)
        return session

    async def _spawn_session(
        self,
        *,
        session_scope: str,
        working_directory: str,
        requested_resume: Optional[str],
        model: Optional[str],
        permission_mode: str,
        normalized_dirs: tuple[str, ...],
        log_prefix: str,
    ) -> _LivePtySession:
        """Spawn a new interactive Claude PTY process."""
        master_fd, slave_fd = os.openpty()
        os.set_blocking(master_fd, False)
        session_id = requested_resume or str(uuid.uuid4())

        cmd = ["claude"]
        if model:
            claude_model, claude_effort = parse_claude_model_effort(model)
            cmd.extend(["--model", claude_model])
            if claude_effort:
                cmd.extend(["--effort", claude_effort])
        cmd.extend(["--permission-mode", permission_mode])
        for directory in normalized_dirs:
            cmd.extend(["--add-dir", directory])
        if requested_resume:
            cmd.extend(["--resume", requested_resume])
        else:
            cmd.extend(["--session-id", session_id])

        logger.info(
            f"{log_prefix}Starting live Claude PTY for scope {session_scope} "
            f"(session_id={session_id})"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=working_directory,
            )
        finally:
            os.close(slave_fd)

        return _LivePtySession(
            scope=session_scope,
            process=process,
            master_fd=master_fd,
            session_id=session_id,
            working_directory=working_directory,
            model=model,
            permission_mode=permission_mode,
            added_dirs=normalized_dirs,
        )

    async def _terminate_session(self, session: _LivePtySession) -> None:
        """Stop process and close PTY file descriptors for one session."""
        try:
            if session.is_running():
                session.process.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    session.process.terminate()
                    try:
                        await asyncio.wait_for(session.process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        session.process.kill()
                        await asyncio.wait_for(session.process.wait(), timeout=1.0)
        except Exception as e:
            logger.debug(f"Failed to terminate PTY session {session.scope}: {e}")
        finally:
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    async def _send_ctrl_c(self, session: _LivePtySession) -> None:
        """Send Ctrl-C to a PTY session."""
        async with session.write_lock:
            try:
                os.write(session.master_fd, b"\x03")
            except OSError as e:
                logger.debug(
                    f"Failed to send Ctrl-C to PTY session {session.scope}: {e}"
                )

    async def _write_text(self, session: _LivePtySession, text: str) -> None:
        """Write text to PTY stdin."""
        async with session.write_lock:
            os.write(session.master_fd, text.encode("utf-8"))
            session.last_activity = monotonic()

    async def _read_chunk(
        self, session: _LivePtySession, timeout_seconds: float
    ) -> Optional[str]:
        """Read a chunk from PTY; None indicates timeout, empty string indicates EOF."""
        if not session.is_running():
            return ""
        deadline = monotonic() + timeout_seconds
        while True:
            try:
                data = os.read(session.master_fd, 4096)
            except BlockingIOError:
                if monotonic() >= deadline:
                    return None
                await asyncio.sleep(0.05)
                continue
            except OSError as e:
                if e.errno in {errno.EIO, errno.EBADF}:
                    return ""
                raise

            if not data:
                return ""
            return data.decode("utf-8", errors="replace")

    async def _drain_pending_output(
        self, session: _LivePtySession, timeout_seconds: float
    ) -> None:
        """Drain any immediately available PTY output to reduce cross-turn bleed."""
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            chunk = await self._read_chunk(session, timeout_seconds=0.05)
            if chunk is None:
                continue
            if chunk == "":
                break

    @staticmethod
    def _normalize_terminal_chunk(text: str) -> str:
        """Normalize raw terminal bytes into readable text."""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = ANSI_ESCAPE_RE.sub("", normalized)
        normalized = normalized.replace("\x00", "")
        normalized = "".join(
            ch for ch in normalized if ch == "\n" or ch == "\t" or ord(ch) >= 32
        )
        return normalized

    @staticmethod
    def _strip_first_prompt_echo(text: str, prompt: str) -> str:
        """Remove the first echoed prompt text from output chunks."""
        prompt_text = prompt.strip()
        if not prompt_text:
            return text
        if text.startswith(prompt_text):
            stripped = text[len(prompt_text) :]
            return stripped[1:] if stripped.startswith("\n") else stripped
        return text

    @staticmethod
    def _finalize_output_text(output: str) -> str:
        """Trim prompt-like suffixes and normalize final output text."""
        trimmed = output.rstrip()
        for marker in ("\n> ", "\n>"):
            if trimmed.endswith(marker):
                trimmed = trimmed[: -len(marker)].rstrip()
        return trimmed

    @staticmethod
    def _tail_has_prompt_marker(text: str) -> bool:
        """Return True when terminal tail looks like Claude's input prompt."""
        return bool(PROMPT_MARKER_RE.search(text))

    async def _get_scope_lock(self, session_scope: str) -> asyncio.Lock:
        """Return scope-scoped lock used for PTY session creation/replacement."""
        async with self._sessions_lock:
            lock = self._scope_locks.get(session_scope)
            if lock is None:
                lock = asyncio.Lock()
                self._scope_locks[session_scope] = lock
            return lock

    @staticmethod
    def _is_uuid(value: Optional[str]) -> bool:
        """Return True when value is a valid UUID string."""
        if not value:
            return False
        try:
            uuid.UUID(str(value))
        except ValueError:
            return False
        return True
