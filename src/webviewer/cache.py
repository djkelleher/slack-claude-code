"""Cache for web viewer content stored in SQLite."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from src.config import config


@dataclass
class WebViewerContent:
    """Stored content for web viewing."""

    id: str
    content_type: str  # 'code' or 'diff'
    file_path: Optional[str]
    content: str  # For code: file content; for diff: old content
    new_content: Optional[str]  # For diff: new content
    tool_name: Optional[str]
    created_at: datetime
    expires_at: datetime


class WebViewerCache:
    """Cache for storing and retrieving web viewer content from SQLite."""

    def __init__(self, db_path: str):
        """Initialize the cache with database path.

        Parameters
        ----------
        db_path : str
            Path to the SQLite database.
        """
        self.db_path = db_path

    async def store(
        self,
        content_type: str,
        content: str,
        file_path: Optional[str] = None,
        new_content: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> str:
        """Store content and return the content ID.

        Parameters
        ----------
        content_type : str
            Type of content ('code' or 'diff').
        content : str
            The content to store. For diffs, this is the old content.
        file_path : Optional[str]
            Original file path for syntax detection.
        new_content : Optional[str]
            For diffs, the new content.
        tool_name : Optional[str]
            Tool name (Read, Edit, Write).

        Returns
        -------
        str
            The generated content ID.
        """
        content_id = str(uuid.uuid4())
        ttl = config.timeouts.webviewer.content_ttl
        expires_at = datetime.utcnow() + timedelta(seconds=ttl)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO web_viewer_content
                   (id, content_type, file_path, content, new_content, tool_name, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (content_id, content_type, file_path, content, new_content, tool_name, expires_at),
            )
            await db.commit()

        return content_id

    async def get(self, content_id: str) -> Optional[WebViewerContent]:
        """Retrieve content by ID.

        Parameters
        ----------
        content_id : str
            The content ID to retrieve.

        Returns
        -------
        Optional[WebViewerContent]
            The content if found and not expired, None otherwise.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT id, content_type, file_path, content, new_content,
                          tool_name, created_at, expires_at
                   FROM web_viewer_content
                   WHERE id = ? AND expires_at > datetime('now')""",
                (content_id,),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            return WebViewerContent(
                id=row["id"],
                content_type=row["content_type"],
                file_path=row["file_path"],
                content=row["content"],
                new_content=row["new_content"],
                tool_name=row["tool_name"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
            )

    async def cleanup(self) -> int:
        """Remove expired entries.

        Returns
        -------
        int
            Number of entries removed.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM web_viewer_content WHERE expires_at <= datetime('now')"
            )
            await db.commit()
            return cursor.rowcount
