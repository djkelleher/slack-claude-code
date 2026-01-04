import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import json
from datetime import datetime
from typing import Optional
from .models import Session, CommandHistory, ParallelJob, QueueItem

# Default timeout for database operations (seconds)
DB_TIMEOUT = 30.0


class DatabaseRepository:
    def __init__(self, db_path: str, timeout: float = DB_TIMEOUT):
        self.db_path = db_path
        self.timeout = timeout

    def _get_connection(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path)

    @asynccontextmanager
    async def _transact(self):
        """Provide a connection with automatic commit on success.

        Usage:
            async with self._transact() as db:
                await db.execute(...)
                # commit happens automatically on exit
        """
        async with self._get_connection() as db:
            yield db
            await db.commit()

    async def _with_timeout(self, coro, timeout: float = None):
        """Wrap a coroutine with a timeout to prevent hanging operations."""
        return await asyncio.wait_for(coro, timeout=timeout or self.timeout)

    # Session operations
    async def get_or_create_session(self, channel_id: str, default_cwd: str = "~") -> Session:
        """Get existing session for channel or create a new one.

        Uses INSERT OR IGNORE to avoid race conditions when multiple
        concurrent requests try to create the same session.
        """
        async with self._get_connection() as db:
            # Atomic insert-or-ignore (UNIQUE constraint on channel_id handles duplicates)
            await db.execute(
                """INSERT OR IGNORE INTO sessions (channel_id, working_directory)
                   VALUES (?, ?)""",
                (channel_id, default_cwd),
            )
            # Update last_active timestamp
            await db.execute(
                "UPDATE sessions SET last_active = ? WHERE channel_id = ?",
                (datetime.now().isoformat(), channel_id),
            )
            await db.commit()

            # Fetch the session (guaranteed to exist now)
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE channel_id = ?", (channel_id,)
            )
            row = await cursor.fetchone()
            return Session.from_row(row)

    async def update_session_cwd(self, channel_id: str, cwd: str) -> None:
        """Update the working directory for a session."""
        async with self._get_connection() as db:
            await db.execute(
                "UPDATE sessions SET working_directory = ?, last_active = ? WHERE channel_id = ?",
                (cwd, datetime.now().isoformat(), channel_id),
            )
            await db.commit()

    async def update_session_claude_id(self, channel_id: str, claude_session_id: str) -> None:
        """Update the Claude session ID for resume functionality."""
        async with self._get_connection() as db:
            await db.execute(
                "UPDATE sessions SET claude_session_id = ?, last_active = ? WHERE channel_id = ?",
                (claude_session_id, datetime.now().isoformat(), channel_id),
            )
            await db.commit()

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
                    (status, output, error_message, datetime.now().isoformat(), command_id),
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
            cursor = await db.execute(
                "SELECT * FROM command_history WHERE id = ?", (command_id,)
            )
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
                (session_id, channel_id, job_type, json.dumps(config), "[]", message_ts),
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
                    params.append(datetime.now().isoformat())

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
                params.append(job_id)
                await db.execute(
                    f"UPDATE parallel_jobs SET {', '.join(updates)} WHERE id = ?",
                    tuple(params),
                )
                await db.commit()

    async def get_parallel_job(self, job_id: int) -> Optional[ParallelJob]:
        """Get a parallel job by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM parallel_jobs WHERE id = ?", (job_id,)
            )
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
                (datetime.now().isoformat(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    # Queue operations
    async def add_to_queue(
        self, session_id: int, channel_id: str, prompt: str
    ) -> QueueItem:
        """Add a command to the FIFO queue."""
        async with self._get_connection() as db:
            # Get next position for this channel
            cursor = await db.execute(
                """SELECT COALESCE(MAX(position), 0) + 1
                   FROM queue_items WHERE channel_id = ?""",
                (channel_id,),
            )
            position = (await cursor.fetchone())[0]

            cursor = await db.execute(
                """INSERT INTO queue_items
                   (session_id, channel_id, prompt, position, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (session_id, channel_id, prompt, position),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM queue_items WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row)

    async def get_pending_queue_items(self, channel_id: str) -> list[QueueItem]:
        """Get all pending queue items for a channel, ordered by position."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT * FROM queue_items
                   WHERE channel_id = ? AND status = 'pending'
                   ORDER BY position ASC""",
                (channel_id,),
            )
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def get_queue_item(self, item_id: int) -> Optional[QueueItem]:
        """Get a queue item by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM queue_items WHERE id = ?", (item_id,)
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    async def update_queue_item_status(
        self,
        item_id: int,
        status: str,
        output: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update queue item status."""
        async with self._get_connection() as db:
            if status == "running":
                await db.execute(
                    "UPDATE queue_items SET status = ?, started_at = ? WHERE id = ?",
                    (status, datetime.now().isoformat(), item_id),
                )
            elif status in ("completed", "failed", "cancelled"):
                await db.execute(
                    """UPDATE queue_items
                       SET status = ?, output = ?, error_message = ?, completed_at = ?
                       WHERE id = ?""",
                    (status, output, error_message, datetime.now().isoformat(), item_id),
                )
            else:
                await db.execute(
                    "UPDATE queue_items SET status = ? WHERE id = ?",
                    (status, item_id),
                )
            await db.commit()

    async def remove_queue_item(self, item_id: int) -> bool:
        """Remove a queue item (only if pending)."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_items WHERE id = ? AND status = 'pending'",
                (item_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def clear_queue(self, channel_id: str) -> int:
        """Clear all pending queue items for a channel."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_items WHERE channel_id = ? AND status = 'pending'",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount

    async def get_running_queue_item(self, channel_id: str) -> Optional[QueueItem]:
        """Get the currently running queue item for a channel."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM queue_items WHERE channel_id = ? AND status = 'running'",
                (channel_id,),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None
