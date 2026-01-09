"""Cache for storing detailed outputs for on-demand viewing.

Stores detailed command outputs temporarily so they can be displayed
in a modal when the user clicks "Show Details" button.
"""

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CachedDetail:
    """A cached detailed output entry."""

    command_id: int
    content: str
    created_at: float


class DetailCache:
    """Simple in-memory cache for detailed outputs.

    Automatically expires entries after max_age_seconds.
    """

    _cache: dict[int, CachedDetail] = {}
    _max_age_seconds: int = 3600  # 1 hour default

    @classmethod
    def store(cls, command_id: int, content: str) -> None:
        """Store detailed output for a command.

        Parameters
        ----------
        command_id : int
            The command history ID.
        content : str
            The detailed output content.
        """
        cls._cache[command_id] = CachedDetail(
            command_id=command_id,
            content=content,
            created_at=time.time(),
        )
        # Clean up old entries
        cls._cleanup()

    @classmethod
    def get(cls, command_id: int) -> Optional[str]:
        """Retrieve detailed output for a command.

        Parameters
        ----------
        command_id : int
            The command history ID.

        Returns
        -------
        str or None
            The detailed output if found and not expired, None otherwise.
        """
        entry = cls._cache.get(command_id)
        if not entry:
            return None

        # Check if expired
        if time.time() - entry.created_at > cls._max_age_seconds:
            del cls._cache[command_id]
            return None

        return entry.content

    @classmethod
    def _cleanup(cls) -> None:
        """Remove expired entries."""
        now = time.time()
        expired = [
            cmd_id
            for cmd_id, entry in cls._cache.items()
            if now - entry.created_at > cls._max_age_seconds
        ]
        for cmd_id in expired:
            del cls._cache[cmd_id]

    @classmethod
    def clear(cls) -> None:
        """Clear all cached entries."""
        cls._cache.clear()
