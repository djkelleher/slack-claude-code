import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from ..config import config
from .models import (
    CommandHistory,
    GitCheckpoint,
    NotificationSettings,
    ParallelJob,
    QueueControl,
    QueueItem,
    QueueScheduledEvent,
    Session,
    UploadedFile,
    WorkspaceLease,
)

# Default timeout for database operations (seconds)
DB_TIMEOUT = 30.0


class DatabaseRepository:
    _SCOPED_THREAD_WHERE = """channel_id = ? AND (
                       (thread_ts = ? AND ? IS NOT NULL) OR
                       (thread_ts IS NULL AND ? IS NULL)
                   )"""
    _SESSION_SCOPE_WHERE = _SCOPED_THREAD_WHERE
    _QUEUE_SCOPE_WHERE = _SCOPED_THREAD_WHERE
    _QUEUE_ITEM_SELECT = """id, session_id, channel_id, thread_ts, prompt,
                       working_directory_override, parallel_group_id, parallel_limit,
                       status, output, error_message, position, message_ts,
                       created_at, started_at, completed_at"""
    _SESSION_SELECT = """id, channel_id, thread_ts, working_directory,
                      claude_session_id, permission_mode, created_at, last_active,
                      model, added_dirs, codex_session_id, sandbox_mode, approval_mode"""
    _QUEUE_SCHEDULED_EVENT_SELECT = """id, channel_id, thread_ts, action, execute_at,
                                   status, error_message, created_at, executed_at"""
    _WORKSPACE_LEASE_SELECT = """id, session_id, channel_id, thread_ts, session_scope,
                              execution_id, repo_root, target_worktree_path, target_branch,
                              leased_root, leased_cwd, base_cwd, relative_subdir, lease_kind,
                              worktree_name, worktree_origin, merge_status, status,
                              created_at, released_at"""

    def __init__(self, db_path: str, timeout: float = DB_TIMEOUT):
        self.db_path = db_path
        self.timeout = timeout
        self._initialized = False

    @staticmethod
    def _normalize_thread_ts(thread_ts: Optional[str]) -> Optional[str]:
        """Normalize blank thread timestamps to None for stable session scope."""
        if thread_ts is None:
            return None
        normalized = thread_ts.strip()
        return normalized or None

    @staticmethod
    def _scope_params(channel_id: str, thread_ts: Optional[str]) -> tuple[Optional[str], ...]:
        """Return standard SQL parameters for channel/thread scoped queries."""
        normalized_thread_ts = DatabaseRepository._normalize_thread_ts(thread_ts)
        return (
            channel_id,
            normalized_thread_ts,
            normalized_thread_ts,
            normalized_thread_ts,
        )

    @staticmethod
    def _session_scope_params(
        channel_id: str, thread_ts: Optional[str]
    ) -> tuple[Optional[str], ...]:
        """Return standard SQL parameters for channel/thread scoped session queries."""
        return DatabaseRepository._scope_params(channel_id, thread_ts)

    @staticmethod
    def _queue_scope_params(channel_id: str, thread_ts: Optional[str]) -> tuple[Optional[str], ...]:
        """Return standard SQL parameters for channel/thread scoped queue queries."""
        return DatabaseRepository._scope_params(channel_id, thread_ts)

    def _get_connection(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path, timeout=self.timeout)

    async def _ensure_wal_mode(self, db: aiosqlite.Connection) -> None:
        """Apply per-connection pragmas and initialize persistent WAL mode once."""
        await db.execute("PRAGMA busy_timeout=30000")  # 30 second timeout for busy
        if not self._initialized:
            await db.execute("PRAGMA journal_mode=WAL")
            self._initialized = True

    async def _select_best_session_row(
        self, db: aiosqlite.Connection, channel_id: str, thread_ts: Optional[str]
    ) -> tuple | None:
        """Return the preferred session row for a channel/thread scope."""
        cursor = await db.execute(
            f"""SELECT {self._SESSION_SELECT}
               FROM sessions
               WHERE {self._SESSION_SCOPE_WHERE}
               ORDER BY
                   (CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) DESC,
                   ((CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) +
                    (CASE WHEN codex_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                    (CASE WHEN claude_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                    (CASE WHEN permission_mode IS NOT NULL THEN 1 ELSE 0 END)) DESC,
                   last_active DESC,
                   id DESC
               LIMIT 1""",
            self._session_scope_params(channel_id, thread_ts),
        )
        return await cursor.fetchone()

    @asynccontextmanager
    async def _transact(self):
        """Provide a connection with automatic commit on success.

        Usage:
            async with self._transact() as db:
                await db.execute(...)
                # commit happens automatically on exit
        """
        async with self._get_connection() as db:
            await self._ensure_wal_mode(db)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # Session operations
    async def get_or_create_session(
        self, channel_id: str, thread_ts: Optional[str] = None, default_cwd: str = "~"
    ) -> Session:
        """Get existing session for channel/thread or create a new one.

        Args:
            channel_id: Slack channel ID
            thread_ts: Slack thread timestamp (None for channel-level session)
            default_cwd: Default working directory for new sessions
        """
        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        async with self._transact() as db:
            now_iso = datetime.now(timezone.utc).isoformat()

            # Find best existing session for this channel/thread pair.
            # If duplicate NULL-thread rows exist, prefer the most populated one.
            row = await self._select_best_session_row(db, channel_id, normalized_thread_ts)

            # Update existing session activity and return it.
            if row is not None:
                await db.execute(
                    "UPDATE sessions SET last_active = ? WHERE id = ?",
                    (now_iso, row[0]),
                )
                # Refresh to return DB-normalized values.
                cursor = await db.execute(
                    f"""SELECT {self._SESSION_SELECT}
                       FROM sessions
                       WHERE id = ?""",
                    (row[0],),
                )
                updated_row = await cursor.fetchone()
                if updated_row is None:
                    raise RuntimeError(
                        f"Failed to load updated session {row[0]} for channel {channel_id}"
                    )
                return Session.from_row(updated_row)

            # Create new session when none exists.
            model = config.DEFAULT_MODEL
            working_directory = default_cwd
            permission_mode = None
            added_dirs_json = None
            claude_session_id = None
            codex_session_id = None
            sandbox_mode = config.CODEX_SANDBOX_MODE
            approval_mode = config.CODEX_APPROVAL_MODE
            if normalized_thread_ts is not None:
                # New thread sessions branch from the channel-level session context.
                channel_cursor = await db.execute(
                    """SELECT working_directory, model, permission_mode, added_dirs,
                              claude_session_id, codex_session_id, sandbox_mode, approval_mode
                       FROM sessions
                       WHERE channel_id = ? AND thread_ts IS NULL
                       ORDER BY last_active DESC, id DESC
                       LIMIT 1""",
                    (channel_id,),
                )
                channel_row = await channel_cursor.fetchone()
                if channel_row:
                    if channel_row[0]:
                        working_directory = channel_row[0]
                    if channel_row[1] is not None:
                        model = channel_row[1]
                    permission_mode = channel_row[2]
                    added_dirs_json = channel_row[3]
                    claude_session_id = channel_row[4]
                    codex_session_id = channel_row[5]
                    sandbox_mode = channel_row[6] or sandbox_mode
                    approval_mode = channel_row[7] or approval_mode

            try:
                cursor = await db.execute(
                    """INSERT INTO sessions (
                           channel_id, thread_ts, working_directory, model, permission_mode,
                           added_dirs, claude_session_id, codex_session_id, sandbox_mode,
                           approval_mode, last_active
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        channel_id,
                        normalized_thread_ts,
                        working_directory,
                        model,
                        permission_mode,
                        added_dirs_json,
                        claude_session_id,
                        codex_session_id,
                        sandbox_mode,
                        approval_mode,
                        now_iso,
                    ),
                )
            except aiosqlite.IntegrityError:
                existing_row = await self._select_best_session_row(
                    db, channel_id, normalized_thread_ts
                )
                if existing_row is None:
                    raise
                await db.execute(
                    "UPDATE sessions SET last_active = ? WHERE id = ?",
                    (now_iso, existing_row[0]),
                )
                return Session.from_row(existing_row)
            session_id = cursor.lastrowid
            if session_id is None:
                raise RuntimeError(f"Failed to create session for channel {channel_id}")

            cursor = await db.execute(
                f"""SELECT {self._SESSION_SELECT}
                   FROM sessions
                   WHERE id = ?""",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(
                    f"Failed to load created session {session_id} for channel {channel_id}"
                )
            return Session.from_row(row)

    async def update_session_cwd(self, channel_id: str, thread_ts: Optional[str], cwd: str) -> None:
        """Update the working directory for a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET working_directory = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    cwd,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_claude_id(
        self, channel_id: str, thread_ts: Optional[str], claude_session_id: str
    ) -> None:
        """Update the Claude session ID for resume functionality."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET claude_session_id = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    claude_session_id,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def clear_session_claude_id(
        self, channel_id: str, thread_ts: Optional[str] = None
    ) -> None:
        """Clear the Claude session ID to start fresh (used by /clear command)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET claude_session_id = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_mode(
        self, channel_id: str, thread_ts: Optional[str], permission_mode: str
    ) -> None:
        """Update the permission mode for a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET permission_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    permission_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_model(
        self, channel_id: str, thread_ts: Optional[str], model: Optional[str]
    ) -> None:
        """Update the model for a session."""
        async with self._transact() as db:
            normalized_thread_ts = self._normalize_thread_ts(thread_ts)
            now_iso = datetime.now(timezone.utc).isoformat()
            cursor = await db.execute(
                f"""UPDATE sessions SET model = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    model,
                    now_iso,
                    *self._session_scope_params(channel_id, normalized_thread_ts),
                ),
            )
            if cursor.rowcount == 0:
                await db.execute(
                    """INSERT INTO sessions (
                           channel_id, thread_ts, working_directory, model, last_active
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        channel_id,
                        normalized_thread_ts,
                        config.DEFAULT_WORKING_DIR,
                        model,
                        now_iso,
                    ),
                )

    async def get_channel_model_selections(self) -> dict[str, str]:
        """Return latest non-null channel-level model selections keyed by channel ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT channel_id, model
                   FROM sessions
                   WHERE thread_ts IS NULL AND model IS NOT NULL
                   ORDER BY last_active DESC, id DESC"""
            )
            rows = await cursor.fetchall()

        selections: dict[str, str] = {}
        for channel_id, model in rows:
            if channel_id not in selections and model is not None:
                selections[channel_id] = model
        return selections

    async def restore_channel_model_selections(self) -> dict[str, str]:
        """Ensure channel sessions are initialized with persisted channel model selections."""
        selections = await self.get_channel_model_selections()
        for channel_id, model in selections.items():
            await self.update_session_model(channel_id, None, model)
        return selections

    async def add_session_dir(
        self, channel_id: str, thread_ts: Optional[str], directory: str
    ) -> list:
        """Add a directory to the session's added_dirs list.

        Returns the updated list of directories.
        """
        async with self._transact() as db:
            # Get current directories
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            current_dirs = json.loads(row[0]) if row and row[0] else []

            # Add directory if not already present
            if directory not in current_dirs:
                current_dirs.append(directory)

            # Update database
            cursor = await db.execute(
                f"""UPDATE sessions SET added_dirs = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    json.dumps(current_dirs),
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(f"Session not found for channel {channel_id} thread {thread_ts}")
            return current_dirs

    async def remove_session_dir(
        self, channel_id: str, thread_ts: Optional[str], directory: str
    ) -> list:
        """Remove a directory from the session's added_dirs list.

        Returns the updated list of directories.
        """
        async with self._transact() as db:
            # Get current directories
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            current_dirs = json.loads(row[0]) if row and row[0] else []

            # Remove directory if present
            if directory in current_dirs:
                current_dirs.remove(directory)

            # Update database
            cursor = await db.execute(
                f"""UPDATE sessions SET added_dirs = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    json.dumps(current_dirs) if current_dirs else None,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(f"Session not found for channel {channel_id} thread {thread_ts}")
            return current_dirs

    async def clear_session_dirs(self, channel_id: str, thread_ts: Optional[str]) -> None:
        """Clear all added directories from a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET added_dirs = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def get_session_dirs(self, channel_id: str, thread_ts: Optional[str]) -> list:
        """Get the list of added directories for a session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            return json.loads(row[0]) if row and row[0] else []

    async def get_session_by_id(self, session_id: int) -> Optional[Session]:
        """Get a session by its database ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT id, channel_id, thread_ts, working_directory,
                          claude_session_id, permission_mode, created_at, last_active,
                          model, added_dirs, codex_session_id, sandbox_mode, approval_mode
                   FROM sessions WHERE id = ?""",
                (session_id,),
            )
            row = await cursor.fetchone()
            return Session.from_row(row) if row else None

    async def delete_session(self, channel_id: str, thread_ts: Optional[str] = None) -> bool:
        """Delete a specific session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""DELETE FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount > 0

    # Command history operations
    async def add_command(self, session_id: int, command: str) -> CommandHistory:
        """Add a new command to history."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "INSERT INTO command_history (session_id, command, status) VALUES (?, ?, 'pending')",
                (session_id, command),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM command_history WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return CommandHistory.from_row(row)

    async def update_command_status(
        self,
        command_id: int,
        status: str,
        output: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update command status and optionally output."""
        async with self._get_connection() as db:
            if status in ("completed", "failed", "cancelled"):
                await db.execute(
                    """UPDATE command_history
                       SET status = ?, output = ?, error_message = ?, completed_at = ?
                       WHERE id = ?""",
                    (
                        status,
                        output,
                        error_message,
                        datetime.now(timezone.utc).isoformat(),
                        command_id,
                    ),
                )
            else:
                await db.execute(
                    "UPDATE command_history SET status = ? WHERE id = ?",
                    (status, command_id),
                )
            await db.commit()

    async def append_command_output(self, command_id: int, output_chunk: str) -> None:
        """Append output chunk to command (for streaming)."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT output FROM command_history WHERE id = ?", (command_id,)
            )
            row = await cursor.fetchone()
            current_output = row[0] or "" if row else ""

            await db.execute(
                "UPDATE command_history SET output = ? WHERE id = ?",
                (current_output + output_chunk, command_id),
            )
            await db.commit()

    async def store_command_detailed_output(self, command_id: int, detailed_output: str) -> None:
        """Persist detailed output for later on-demand viewing."""
        async with self._get_connection() as db:
            await db.execute(
                "UPDATE command_history SET detailed_output = ? WHERE id = ?",
                (detailed_output, command_id),
            )
            await db.commit()

    async def get_command_detailed_output(self, command_id: int) -> Optional[str]:
        """Return persisted detailed output for a command, if available."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT detailed_output FROM command_history WHERE id = ?",
                (command_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else None

    async def get_command_history(
        self, session_id: int, limit: int = 10, offset: int = 0
    ) -> tuple[list[CommandHistory], int]:
        """Get paginated command history for a session."""
        async with self._get_connection() as db:
            # Get total count
            cursor = await db.execute(
                "SELECT COUNT(*) FROM command_history WHERE session_id = ?",
                (session_id,),
            )
            total = (await cursor.fetchone())[0]

            # Get paginated results
            cursor = await db.execute(
                """SELECT * FROM command_history
                   WHERE session_id = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (session_id, limit, offset),
            )
            rows = await cursor.fetchall()
            return [CommandHistory.from_row(row) for row in rows], total

    async def get_command_by_id(self, command_id: int) -> Optional[CommandHistory]:
        """Get a specific command by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute("SELECT * FROM command_history WHERE id = ?", (command_id,))
            row = await cursor.fetchone()
            return CommandHistory.from_row(row) if row else None

    # Parallel job operations
    async def create_parallel_job(
        self,
        session_id: int,
        channel_id: str,
        job_type: str,
        config: dict,
        message_ts: Optional[str] = None,
    ) -> ParallelJob:
        """Create a new parallel job."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO parallel_jobs
                   (session_id, channel_id, job_type, config, results, message_ts, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    session_id,
                    channel_id,
                    job_type,
                    json.dumps(config),
                    "[]",
                    message_ts,
                ),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM parallel_jobs WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return ParallelJob.from_row(row)

    async def update_parallel_job(
        self,
        job_id: int,
        status: Optional[str] = None,
        results: Optional[list] = None,
        aggregation_output: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> None:
        """Update parallel job fields."""
        async with self._get_connection() as db:
            updates = []
            params = []

            if status:
                updates.append("status = ?")
                params.append(status)
                if status in ("completed", "failed", "cancelled"):
                    updates.append("completed_at = ?")
                    params.append(datetime.now(timezone.utc).isoformat())

            if results is not None:
                updates.append("results = ?")
                params.append(json.dumps(results))

            if aggregation_output is not None:
                updates.append("aggregation_output = ?")
                params.append(aggregation_output)

            if message_ts is not None:
                updates.append("message_ts = ?")
                params.append(message_ts)

            if updates:
                # Build SQL safely with placeholders
                sql = "UPDATE parallel_jobs SET " + ", ".join(updates) + " WHERE id = ?"
                params.append(job_id)
                await db.execute(sql, tuple(params))
                await db.commit()

    async def get_parallel_job(self, job_id: int) -> Optional[ParallelJob]:
        """Get a parallel job by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute("SELECT * FROM parallel_jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            return ParallelJob.from_row(row) if row else None

    async def get_active_jobs(self, channel_id: Optional[str] = None) -> list[ParallelJob]:
        """Get all active (pending/running) jobs, optionally filtered by channel."""
        async with self._get_connection() as db:
            if channel_id:
                cursor = await db.execute(
                    """SELECT * FROM parallel_jobs
                       WHERE status IN ('pending', 'running') AND channel_id = ?
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM parallel_jobs
                       WHERE status IN ('pending', 'running')
                       ORDER BY created_at DESC"""
                )
            rows = await cursor.fetchall()
            return [ParallelJob.from_row(row) for row in rows]

    async def cancel_job(self, job_id: int) -> bool:
        """Cancel a job if it's still active."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE parallel_jobs
                   SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('pending', 'running')""",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    # Workspace lease operations
    async def create_workspace_lease(
        self,
        *,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        session_scope: str,
        execution_id: str,
        repo_root: Optional[str],
        target_worktree_path: Optional[str],
        target_branch: Optional[str],
        leased_root: str,
        leased_cwd: str,
        base_cwd: str,
        relative_subdir: Optional[str],
        lease_kind: str,
        worktree_name: Optional[str] = None,
        worktree_origin: Optional[str] = None,
        merge_status: Optional[str] = None,
        status: str = "active",
    ) -> WorkspaceLease:
        """Create and return a workspace lease row."""
        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._transact() as db:
            cursor = await db.execute(
                """INSERT INTO workspace_leases (
                       session_id, channel_id, thread_ts, session_scope, execution_id,
                       repo_root, target_worktree_path, target_branch, leased_root, leased_cwd,
                       base_cwd, relative_subdir, lease_kind, worktree_name, worktree_origin,
                       merge_status, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    channel_id,
                    normalized_thread_ts,
                    session_scope,
                    execution_id,
                    repo_root,
                    target_worktree_path,
                    target_branch,
                    leased_root,
                    leased_cwd,
                    base_cwd,
                    relative_subdir,
                    lease_kind,
                    worktree_name,
                    worktree_origin,
                    merge_status,
                    status,
                    now_iso,
                ),
            )
            lease_id = cursor.lastrowid
            if lease_id is None:
                raise RuntimeError(f"Failed to create workspace lease for execution {execution_id}")
            cursor = await db.execute(
                f"SELECT {self._WORKSPACE_LEASE_SELECT} FROM workspace_leases WHERE id = ?",
                (lease_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(f"Failed to load workspace lease {lease_id}")
            return WorkspaceLease.from_row(row)

    async def get_workspace_lease_by_execution(self, execution_id: str) -> Optional[WorkspaceLease]:
        """Get the most recent workspace lease for an execution."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._WORKSPACE_LEASE_SELECT}
                    FROM workspace_leases
                    WHERE execution_id = ?
                    ORDER BY id DESC
                    LIMIT 1""",
                (execution_id,),
            )
            row = await cursor.fetchone()
            return WorkspaceLease.from_row(row) if row else None

    async def get_active_workspace_lease_by_root(
        self, leased_root: str
    ) -> Optional[WorkspaceLease]:
        """Return the active workspace lease for a leased root, if any."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._WORKSPACE_LEASE_SELECT}
                    FROM workspace_leases
                    WHERE leased_root = ? AND status = 'active' AND released_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1""",
                (leased_root,),
            )
            row = await cursor.fetchone()
            return WorkspaceLease.from_row(row) if row else None

    async def list_active_workspace_leases(
        self,
        repo_root: Optional[str] = None,
    ) -> list[WorkspaceLease]:
        """Return active workspace leases, optionally filtered by repo root."""
        async with self._get_connection() as db:
            if repo_root is None:
                cursor = await db.execute(
                    f"""SELECT {self._WORKSPACE_LEASE_SELECT}
                        FROM workspace_leases
                        WHERE status = 'active' AND released_at IS NULL
                        ORDER BY created_at ASC, id ASC"""
                )
            else:
                cursor = await db.execute(
                    f"""SELECT {self._WORKSPACE_LEASE_SELECT}
                        FROM workspace_leases
                        WHERE status = 'active' AND released_at IS NULL AND repo_root = ?
                        ORDER BY created_at ASC, id ASC""",
                    (repo_root,),
                )
            rows = await cursor.fetchall()
            return [WorkspaceLease.from_row(row) for row in rows]

    async def update_workspace_lease(
        self,
        execution_id: str,
        *,
        leased_cwd: Optional[str] = None,
        leased_root: Optional[str] = None,
        worktree_origin: Optional[str] = None,
        merge_status: Optional[str] = None,
        status: Optional[str] = None,
        released: bool = False,
    ) -> bool:
        """Update mutable workspace lease fields by execution ID."""
        updates: list[str] = []
        params: list[object] = []

        if leased_cwd is not None:
            updates.append("leased_cwd = ?")
            params.append(leased_cwd)
        if leased_root is not None:
            updates.append("leased_root = ?")
            params.append(leased_root)
        if worktree_origin is not None:
            updates.append("worktree_origin = ?")
            params.append(worktree_origin)
        if merge_status is not None:
            updates.append("merge_status = ?")
            params.append(merge_status)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if released:
            updates.append("released_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())

        if not updates:
            return False

        async with self._get_connection() as db:
            cursor = await db.execute(
                "UPDATE workspace_leases SET " + ", ".join(updates) + " WHERE execution_id = ?",
                (*params, execution_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def release_workspace_lease(
        self,
        execution_id: str,
        *,
        status: str = "released",
        merge_status: Optional[str] = None,
    ) -> bool:
        """Mark a workspace lease released with optional merge status."""
        return await self.update_workspace_lease(
            execution_id,
            status=status,
            merge_status=merge_status,
            released=True,
        )

    async def mark_workspace_lease_abandoned(
        self, execution_id: str, *, merge_status: Optional[str] = None
    ) -> bool:
        """Mark a workspace lease abandoned."""
        return await self.release_workspace_lease(
            execution_id,
            status="abandoned",
            merge_status=merge_status,
        )

    # Queue operations
    async def add_to_queue(
        self,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        prompt: str,
        working_directory_override: Optional[str] = None,
        parallel_group_id: Optional[str] = None,
        parallel_limit: Optional[int] = None,
        insert_at: Optional[int] = None,
    ) -> QueueItem:
        """Add a command to the FIFO queue."""
        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        normalized_working_directory_override = (
            working_directory_override.strip() if working_directory_override else None
        )
        normalized_parallel_group_id = parallel_group_id.strip() if parallel_group_id else None
        async with self._get_connection() as db:
            await self._ensure_wal_mode(db)
            try:
                # Serialize position assignment per database so concurrent inserts
                # cannot compute the same MAX(position) value.
                await db.execute("BEGIN IMMEDIATE")

                position = await self._next_queue_insert_position(
                    db=db,
                    channel_id=channel_id,
                    thread_ts=normalized_thread_ts,
                    insert_at=insert_at,
                )

                cursor = await db.execute(
                    """INSERT INTO queue_items
                       (session_id, channel_id, thread_ts, prompt, working_directory_override,
                        parallel_group_id, parallel_limit, position, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (
                        session_id,
                        channel_id,
                        normalized_thread_ts,
                        prompt,
                        normalized_working_directory_override,
                        normalized_parallel_group_id,
                        parallel_limit,
                        position,
                    ),
                )
                item_id = cursor.lastrowid
                if item_id is None:
                    raise RuntimeError(
                        f"Failed to add queue item for channel {channel_id} thread {thread_ts}"
                    )

                cursor = await db.execute(
                    f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE id = ?",
                    (item_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError(f"Failed to load queue item #{item_id}")

                await db.commit()
                return QueueItem.from_row(row)
            except Exception:
                await db.rollback()
                raise

    async def add_many_to_queue(
        self,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        queue_entries: list[tuple[str, Optional[str], Optional[str], Optional[int]]],
        replace_pending: bool = False,
        insertion_mode: str = "append",
        insert_at: Optional[int] = None,
    ) -> list[QueueItem]:
        """Add multiple commands to the FIFO queue atomically.

        Parameters
        ----------
        session_id : int
            Session ID owning the queued commands.
        channel_id : str
            Slack channel ID for queue scope.
        thread_ts : str | None
            Slack thread timestamp for queue scope.
        queue_entries : list[tuple[str, Optional[str], Optional[str], Optional[int]]]
            Sequence of (prompt, working_directory_override, parallel_group_id,
            parallel_limit) entries in queue order.
        replace_pending : bool
            When True, replace pending items in the current scope before inserting.
        insertion_mode : str
            One of ``append``, ``prepend``, or ``insert``.
        insert_at : int | None
            One-based pending-queue index used when ``insertion_mode == "insert"``.
        """
        if not queue_entries:
            return []

        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        normalized_entries = [
            (
                prompt,
                override.strip() if override else None,
                parallel_group_id.strip() if parallel_group_id else None,
                parallel_limit,
            )
            for prompt, override, parallel_group_id, parallel_limit in queue_entries
        ]

        async with self._get_connection() as db:
            await self._ensure_wal_mode(db)
            try:
                await db.execute("BEGIN IMMEDIATE")

                scope_params = self._queue_scope_params(channel_id, normalized_thread_ts)
                if replace_pending:
                    await db.execute(
                        "DELETE FROM queue_items WHERE "
                        + self._QUEUE_SCOPE_WHERE
                        + " AND status = 'pending'",
                        scope_params,
                    )
                    insertion_mode = "append"
                    insert_at = None

                first_position = await self._next_queue_insert_position(
                    db=db,
                    channel_id=channel_id,
                    thread_ts=normalized_thread_ts,
                    insert_at=self._resolve_queue_insert_at(insertion_mode, insert_at),
                )

                created_item_ids: list[int] = []
                for offset, (
                    prompt,
                    working_directory_override,
                    parallel_group_id,
                    parallel_limit,
                ) in enumerate(normalized_entries, start=1):
                    cursor = await db.execute(
                        """INSERT INTO queue_items
                           (session_id, channel_id, thread_ts, prompt, working_directory_override,
                            parallel_group_id, parallel_limit, position, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                        (
                            session_id,
                            channel_id,
                            normalized_thread_ts,
                            prompt,
                            working_directory_override,
                            parallel_group_id,
                            parallel_limit,
                            first_position + offset - 1,
                        ),
                    )
                    item_id = cursor.lastrowid
                    if item_id is None:
                        raise RuntimeError(
                            "Failed to add queue item while enqueuing a multi-item plan"
                        )
                    created_item_ids.append(item_id)

                placeholders = ", ".join("?" for _ in created_item_ids)
                cursor = await db.execute(
                    f"""SELECT {self._QUEUE_ITEM_SELECT}
                        FROM queue_items
                        WHERE id IN ({placeholders})
                        ORDER BY position ASC, id ASC""",
                    tuple(created_item_ids),
                )
                rows = await cursor.fetchall()
                if len(rows) != len(created_item_ids):
                    raise RuntimeError(
                        "Failed to load all queued items while enqueuing a multi-item plan"
                    )

                await db.commit()
                return [QueueItem.from_row(row) for row in rows]
            except Exception:
                await db.rollback()
                raise

    def _resolve_queue_insert_at(
        self, insertion_mode: str, insert_at: Optional[int]
    ) -> Optional[int]:
        """Normalize queue insertion mode into a concrete one-based insert index."""
        normalized_mode = (insertion_mode or "append").strip().lower()
        if normalized_mode == "append":
            return None
        if normalized_mode == "prepend":
            return 1
        if normalized_mode == "insert":
            if insert_at is None:
                raise ValueError("insert_at is required when insertion_mode is 'insert'")
            return max(1, int(insert_at))
        raise ValueError(f"Unsupported queue insertion mode: {insertion_mode}")

    async def _next_queue_insert_position(
        self,
        *,
        db,
        channel_id: str,
        thread_ts: Optional[str],
        insert_at: Optional[int],
    ) -> int:
        """Return the DB position to use for a newly inserted pending queue item."""
        scope_params = self._queue_scope_params(channel_id, thread_ts)
        if insert_at is None:
            cursor = await db.execute(
                """SELECT COALESCE(MAX(position), 0) + 1
                   FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE,
                scope_params,
            )
            return int((await cursor.fetchone())[0])

        cursor = await db.execute(
            f"""SELECT position
                FROM queue_items
                WHERE {self._QUEUE_SCOPE_WHERE} AND status = 'pending'
                ORDER BY position ASC, id ASC""",
            scope_params,
        )
        pending_rows = await cursor.fetchall()
        pending_positions = [int(row[0]) for row in pending_rows]
        if not pending_positions:
            cursor = await db.execute(
                """SELECT COALESCE(MAX(position), 0) + 1
                   FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE,
                scope_params,
            )
            return int((await cursor.fetchone())[0])

        zero_based_index = min(max(insert_at - 1, 0), len(pending_positions) - 1)
        target_position = pending_positions[zero_based_index]
        await db.execute(
            "UPDATE queue_items SET position = position + 1 WHERE "
            + self._QUEUE_SCOPE_WHERE
            + " AND status = 'pending' AND position >= ?",
            (*scope_params, target_position),
        )
        return target_position

    async def get_pending_queue_items(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> list[QueueItem]:
        """Get all pending queue items for a session scope, ordered by position."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE
                + """ AND status = 'pending'
                   ORDER BY position ASC, id ASC""",
                self._queue_scope_params(channel_id, thread_ts),
            )
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def get_queue_group_items(
        self,
        channel_id: str,
        thread_ts: Optional[str],
        parallel_group_id: str,
        statuses: Optional[tuple[str, ...]] = None,
    ) -> list[QueueItem]:
        """Get queue items for a parallel group, ordered by position."""
        if not parallel_group_id:
            return []

        async with self._get_connection() as db:
            params: list[object] = list(self._queue_scope_params(channel_id, thread_ts))
            params.append(parallel_group_id)
            sql = (
                f"""SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE
                + " AND parallel_group_id = ?"
            )
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                sql += f" AND status IN ({placeholders})"
                params.extend(statuses)
            sql += " ORDER BY position ASC, id ASC"
            cursor = await db.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def get_queue_item(self, item_id: int) -> Optional[QueueItem]:
        """Get a queue item by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE id = ?",
                (item_id,),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    async def update_queue_item_status(
        self,
        item_id: int,
        status: str,
        output: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update queue item status.

        Returns
        -------
        bool
            True when at least one row is updated, False otherwise.
        """
        async with self._get_connection() as db:
            if status == "running":
                cursor = await db.execute(
                    "UPDATE queue_items SET status = ?, started_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (status, datetime.now(timezone.utc).isoformat(), item_id),
                )
            elif status in ("completed", "failed", "cancelled"):
                cursor = await db.execute(
                    """UPDATE queue_items
                       SET status = ?, output = ?, error_message = ?, completed_at = ?
                       WHERE id = ?""",
                    (
                        status,
                        output,
                        error_message,
                        datetime.now(timezone.utc).isoformat(),
                        item_id,
                    ),
                )
            elif status == "pending":
                cursor = await db.execute(
                    """UPDATE queue_items
                       SET status = ?, output = NULL, error_message = NULL,
                           started_at = NULL, completed_at = NULL
                       WHERE id = ?""",
                    (status, item_id),
                )
            else:
                cursor = await db.execute(
                    "UPDATE queue_items SET status = ? WHERE id = ?",
                    (status, item_id),
                )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_queue_item(
        self,
        item_id: int,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> bool:
        """Remove a queue item (only if pending), optionally constrained to scope."""
        async with self._get_connection() as db:
            if channel_id is None:
                cursor = await db.execute(
                    "DELETE FROM queue_items WHERE id = ? AND status = 'pending'",
                    (item_id,),
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM queue_items WHERE id = ? AND status = 'pending' AND "
                    + self._QUEUE_SCOPE_WHERE,
                    (item_id, *self._queue_scope_params(channel_id, thread_ts)),
                )
            await db.commit()
            return cursor.rowcount > 0

    async def clear_queue(self, channel_id: str, thread_ts: Optional[str]) -> int:
        """Clear all pending queue items for a session scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_items WHERE "
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'pending'",
                self._queue_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount

    async def delete_queue(self, channel_id: str, thread_ts: Optional[str]) -> int:
        """Delete all queue items for a session scope, regardless of status."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_items WHERE " + self._QUEUE_SCOPE_WHERE,
                self._queue_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount

    async def get_running_queue_item(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> Optional[QueueItem]:
        """Get the currently running queue item for a session scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE "
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'running'",
                self._queue_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    async def get_running_queue_items(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> list[QueueItem]:
        """Get all running queue items for a session scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'running' ORDER BY position ASC, id ASC",
                self._queue_scope_params(channel_id, thread_ts),
            )
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def get_completed_queue_items_before_position(
        self,
        channel_id: str,
        thread_ts: Optional[str],
        position: int,
    ) -> list[QueueItem]:
        """Return completed queue items before a given position in scope order."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_ITEM_SELECT}
                    FROM queue_items
                    WHERE {self._QUEUE_SCOPE_WHERE}
                      AND status = 'completed'
                      AND position < ?
                    ORDER BY position ASC, id ASC""",
                (*self._queue_scope_params(channel_id, thread_ts), position),
            )
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def list_pending_queue_scopes(self) -> list[tuple[str, Optional[str]]]:
        """List all channel/thread scopes that currently have pending queue items."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """
                SELECT channel_id, thread_ts
                FROM queue_items
                WHERE status = 'pending'
                GROUP BY channel_id, thread_ts
                ORDER BY channel_id ASC,
                         CASE WHEN thread_ts IS NULL THEN 0 ELSE 1 END,
                         thread_ts ASC
                """
            )
            rows = await cursor.fetchall()
            return [(str(row[0]), self._normalize_thread_ts(row[1])) for row in rows]

    async def list_queue_scopes_for_channel(self, channel_id: str) -> list[Optional[str]]:
        """List queue scopes with activity or non-default control state for a channel."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """
                SELECT DISTINCT thread_ts
                FROM (
                    SELECT thread_ts
                    FROM queue_items
                    WHERE channel_id = ?
                    UNION
                    SELECT thread_ts
                    FROM queue_controls
                    WHERE channel_id = ? AND state != 'running'
                    UNION
                    SELECT thread_ts
                    FROM queue_scheduled_events
                    WHERE channel_id = ? AND status = 'pending'
                )
                ORDER BY CASE WHEN thread_ts IS NULL THEN 0 ELSE 1 END, thread_ts ASC
                """,
                (channel_id, channel_id, channel_id),
            )
            rows = await cursor.fetchall()
            return [self._normalize_thread_ts(row[0]) for row in rows]

    async def get_queue_control(self, channel_id: str, thread_ts: Optional[str]) -> QueueControl:
        """Get queue execution control state for a scope."""
        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT id, channel_id, thread_ts, state, created_at, updated_at
                   FROM queue_controls
                   WHERE """
                + self._QUEUE_SCOPE_WHERE
                + """
                   ORDER BY updated_at DESC, id DESC
                   LIMIT 1""",
                self._queue_scope_params(channel_id, normalized_thread_ts),
            )
            row = await cursor.fetchone()
            if row:
                return QueueControl.from_row(row)
            return QueueControl.default(channel_id, normalized_thread_ts)

    async def update_queue_control_state(
        self, channel_id: str, thread_ts: Optional[str], state: str
    ) -> QueueControl:
        """Create or update queue execution control state for a scope."""
        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        async with self._transact() as db:
            cursor = await db.execute(
                """SELECT id FROM queue_controls
                   WHERE """
                + self._QUEUE_SCOPE_WHERE
                + """
                   ORDER BY updated_at DESC, id DESC
                   LIMIT 1""",
                self._queue_scope_params(channel_id, normalized_thread_ts),
            )
            row = await cursor.fetchone()

            if row:
                await db.execute(
                    """UPDATE queue_controls
                       SET state = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE """
                    + self._QUEUE_SCOPE_WHERE,
                    (
                        state,
                        *self._queue_scope_params(channel_id, normalized_thread_ts),
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO queue_controls (channel_id, thread_ts, state)
                       VALUES (?, ?, ?)""",
                    (channel_id, normalized_thread_ts, state),
                )

        return await self.get_queue_control(channel_id, normalized_thread_ts)

    async def add_queue_scheduled_events(
        self,
        channel_id: str,
        thread_ts: Optional[str],
        events: list[tuple[str, datetime]],
    ) -> list[QueueScheduledEvent]:
        """Create queue scheduled control events for a scope."""
        if not events:
            return []

        normalized_thread_ts = self._normalize_thread_ts(thread_ts)
        async with self._transact() as db:
            created_ids: list[int] = []
            for action, execute_at in events:
                if execute_at.tzinfo is None or execute_at.tzinfo.utcoffset(execute_at) is None:
                    raise ValueError("execute_at must be timezone-aware")
                cursor = await db.execute(
                    """INSERT INTO queue_scheduled_events
                       (channel_id, thread_ts, action, execute_at, status)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (
                        channel_id,
                        normalized_thread_ts,
                        action,
                        execute_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
                event_id = cursor.lastrowid
                if event_id is None:
                    raise RuntimeError("Failed to create queue scheduled event")
                created_ids.append(event_id)

            placeholders = ", ".join("?" for _ in created_ids)
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_SCHEDULED_EVENT_SELECT}
                    FROM queue_scheduled_events
                    WHERE id IN ({placeholders})
                    ORDER BY execute_at ASC, id ASC""",
                tuple(created_ids),
            )
            rows = await cursor.fetchall()
            if len(rows) != len(created_ids):
                raise RuntimeError("Failed to load all queue scheduled events after insert")
            return [QueueScheduledEvent.from_row(row) for row in rows]

    async def get_pending_queue_scheduled_events(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> list[QueueScheduledEvent]:
        """Get pending queue scheduled control events for a scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_SCHEDULED_EVENT_SELECT}
                    FROM queue_scheduled_events
                    WHERE """
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'pending'"
                + " ORDER BY execute_at ASC, id ASC",
                self._queue_scope_params(channel_id, thread_ts),
            )
            rows = await cursor.fetchall()
            return [QueueScheduledEvent.from_row(row) for row in rows]

    async def get_due_queue_scheduled_events(
        self,
        now_utc: datetime,
        limit: int = 50,
    ) -> list[QueueScheduledEvent]:
        """Get pending queue scheduled events due for execution."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if now_utc.tzinfo is None or now_utc.tzinfo.utcoffset(now_utc) is None:
            raise ValueError("now_utc must be timezone-aware")

        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_SCHEDULED_EVENT_SELECT}
                    FROM queue_scheduled_events
                    WHERE status = 'pending' AND execute_at <= ?
                    ORDER BY execute_at ASC, id ASC
                    LIMIT ?""",
                (now_utc.astimezone(timezone.utc).isoformat(), limit),
            )
            rows = await cursor.fetchall()
            return [QueueScheduledEvent.from_row(row) for row in rows]

    async def mark_queue_scheduled_event_executed(self, event_id: int) -> bool:
        """Mark a queue scheduled event as executed."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE queue_scheduled_events
                   SET status = 'executed', error_message = NULL, executed_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_queue_scheduled_event_failed(self, event_id: int, error_message: str) -> bool:
        """Mark a queue scheduled event as failed."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE queue_scheduled_events
                   SET status = 'failed', error_message = ?, executed_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (error_message, datetime.now(timezone.utc).isoformat(), event_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def cancel_queue_scheduled_event(
        self, event_id: int, channel_id: str, thread_ts: Optional[str]
    ) -> bool:
        """Cancel one pending queue scheduled event for a scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE queue_scheduled_events
                   SET status = 'cancelled', error_message = NULL, executed_at = ?
                   WHERE id = ? AND status = 'pending' AND """
                + self._QUEUE_SCOPE_WHERE,
                (
                    datetime.now(timezone.utc).isoformat(),
                    event_id,
                    *self._queue_scope_params(channel_id, thread_ts),
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def cancel_pending_queue_scheduled_events(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> int:
        """Cancel all pending queue scheduled events for a scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE queue_scheduled_events
                   SET status = 'cancelled', error_message = NULL, executed_at = ?
                   WHERE """
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'pending'",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._queue_scope_params(channel_id, thread_ts),
                ),
            )
            await db.commit()
            return cursor.rowcount

    async def delete_pending_queue_scheduled_events(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> int:
        """Delete pending queue scheduled events for a scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_scheduled_events WHERE "
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'pending'",
                self._queue_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount

    # Uploaded file operations
    async def add_uploaded_file(
        self,
        session_id: int,
        slack_file_id: str,
        filename: str,
        local_path: str,
        mimetype: str = "",
        size: int = 0,
    ) -> UploadedFile:
        """Track an uploaded file."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT OR REPLACE INTO uploaded_files
                   (session_id, slack_file_id, filename, local_path, mimetype, size)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, slack_file_id, filename, local_path, mimetype, size),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM uploaded_files WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return UploadedFile.from_row(row)

    async def get_session_uploaded_files(self, session_id: int) -> list[UploadedFile]:
        """Get all uploaded files for a session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT * FROM uploaded_files
                   WHERE session_id = ?
                   ORDER BY uploaded_at DESC""",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [UploadedFile.from_row(row) for row in rows]

    # Git checkpoint operations
    async def create_checkpoint(
        self,
        session_id: int,
        channel_id: str,
        name: str,
        stash_ref: str,
        stash_message: Optional[str] = None,
        description: Optional[str] = None,
        is_auto: bool = False,
    ) -> GitCheckpoint:
        """Create a git checkpoint record."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO git_checkpoints
                   (session_id, channel_id, name, stash_ref, stash_message, description, is_auto)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    channel_id,
                    name,
                    stash_ref,
                    stash_message,
                    description,
                    1 if is_auto else 0,
                ),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM git_checkpoints WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return GitCheckpoint.from_row(row)

    async def get_checkpoints(
        self, channel_id: str, include_auto: bool = False
    ) -> list[GitCheckpoint]:
        """Get checkpoints for a channel."""
        async with self._get_connection() as db:
            if include_auto:
                cursor = await db.execute(
                    """SELECT * FROM git_checkpoints
                       WHERE channel_id = ?
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM git_checkpoints
                       WHERE channel_id = ? AND is_auto = 0
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            rows = await cursor.fetchall()
            return [GitCheckpoint.from_row(row) for row in rows]

    async def get_checkpoint_by_name(self, channel_id: str, name: str) -> Optional[GitCheckpoint]:
        """Get a specific checkpoint by name."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT * FROM git_checkpoints
                   WHERE channel_id = ? AND name = ?
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (channel_id, name),
            )
            row = await cursor.fetchone()
            return GitCheckpoint.from_row(row) if row else None

    async def delete_checkpoint(self, checkpoint_id: int) -> bool:
        """Delete a checkpoint."""
        async with self._get_connection() as db:
            cursor = await db.execute("DELETE FROM git_checkpoints WHERE id = ?", (checkpoint_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def delete_auto_checkpoints(self, channel_id: str) -> int:
        """Delete all auto checkpoints for a channel."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM git_checkpoints WHERE channel_id = ? AND is_auto = 1",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount

    # -------------------------------------------------------------------------
    # Notification Settings
    # -------------------------------------------------------------------------

    async def get_notification_settings(self, channel_id: str) -> "NotificationSettings":
        """
        Get notification settings for a channel.

        Returns default settings (all enabled) if no record exists.
        """
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM notification_settings WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
            if row:
                return NotificationSettings.from_row(row)
            # Return defaults (all notifications enabled)
            return NotificationSettings.default(channel_id)

    async def update_notification_settings(
        self,
        channel_id: str,
        notify_on_completion: bool,
        notify_on_permission: bool,
    ) -> "NotificationSettings":
        """
        Update notification settings for a channel (upsert).

        Creates the record if it doesn't exist.
        """
        async with self._transact() as db:
            # Try to update first
            cursor = await db.execute(
                """UPDATE notification_settings
                   SET notify_on_completion = ?,
                       notify_on_permission = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE channel_id = ?""",
                (notify_on_completion, notify_on_permission, channel_id),
            )

            if cursor.rowcount == 0:
                # Insert new record
                await db.execute(
                    """INSERT INTO notification_settings
                       (channel_id, notify_on_completion, notify_on_permission)
                       VALUES (?, ?, ?)""",
                    (channel_id, notify_on_completion, notify_on_permission),
                )

        # Return the updated settings
        return await self.get_notification_settings(channel_id)

    # -------------------------------------------------------------------------
    # Codex-specific Session Operations
    # -------------------------------------------------------------------------

    async def update_session_codex_id(
        self, channel_id: str, thread_ts: Optional[str], codex_session_id: str
    ) -> None:
        """Update the Codex session ID for resume functionality."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET codex_session_id = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    codex_session_id,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def clear_session_codex_id(
        self, channel_id: str, thread_ts: Optional[str] = None
    ) -> None:
        """Clear the Codex session ID to start fresh."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET codex_session_id = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_sandbox_mode(
        self, channel_id: str, thread_ts: Optional[str], sandbox_mode: str
    ) -> None:
        """Update the sandbox mode for a session (Codex)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET sandbox_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    sandbox_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_approval_mode(
        self, channel_id: str, thread_ts: Optional[str], approval_mode: str
    ) -> None:
        """Update the approval mode for a session (Codex)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET approval_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    approval_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
