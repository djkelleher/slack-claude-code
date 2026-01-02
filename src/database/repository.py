import aiosqlite
import json
from datetime import datetime
from typing import Optional
from .models import Session, CommandHistory, ParallelJob


class DatabaseRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _get_connection(self) -> aiosqlite.Connection:
        return await aiosqlite.connect(self.db_path)

    # Session operations
    async def get_or_create_session(self, channel_id: str, default_cwd: str = "~") -> Session:
        """Get existing session for channel or create a new one."""
        async with await self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE channel_id = ?", (channel_id,)
            )
            row = await cursor.fetchone()

            if row:
                # Update last_active
                await db.execute(
                    "UPDATE sessions SET last_active = ? WHERE channel_id = ?",
                    (datetime.now().isoformat(), channel_id),
                )
                await db.commit()
                return Session.from_row(row)

            # Create new session
            await db.execute(
                "INSERT INTO sessions (channel_id, working_directory) VALUES (?, ?)",
                (channel_id, default_cwd),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM sessions WHERE channel_id = ?", (channel_id,)
            )
            row = await cursor.fetchone()
            return Session.from_row(row)

    async def update_session_cwd(self, channel_id: str, cwd: str) -> None:
        """Update the working directory for a session."""
        async with await self._get_connection() as db:
            await db.execute(
                "UPDATE sessions SET working_directory = ?, last_active = ? WHERE channel_id = ?",
                (cwd, datetime.now().isoformat(), channel_id),
            )
            await db.commit()

    async def update_session_claude_id(self, channel_id: str, claude_session_id: str) -> None:
        """Update the Claude session ID for resume functionality."""
        async with await self._get_connection() as db:
            await db.execute(
                "UPDATE sessions SET claude_session_id = ?, last_active = ? WHERE channel_id = ?",
                (claude_session_id, datetime.now().isoformat(), channel_id),
            )
            await db.commit()

    # Command history operations
    async def add_command(self, session_id: int, command: str) -> CommandHistory:
        """Add a new command to history."""
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM parallel_jobs WHERE id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            return ParallelJob.from_row(row) if row else None

    async def get_active_jobs(self, channel_id: Optional[str] = None) -> list[ParallelJob]:
        """Get all active (pending/running) jobs, optionally filtered by channel."""
        async with await self._get_connection() as db:
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
        async with await self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE parallel_jobs
                   SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('pending', 'running')""",
                (datetime.now().isoformat(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0
