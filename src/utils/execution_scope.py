"""Utilities for deriving stable execution scope identifiers."""

from typing import Optional


def build_session_scope(channel_id: str, thread_ts: Optional[str]) -> str:
    """Build a stable scope key for a channel or thread session."""
    if thread_ts:
        return f"{channel_id}:thread:{thread_ts}"
    return f"{channel_id}:channel"
