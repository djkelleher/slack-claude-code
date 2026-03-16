"""Database layer for SQLite persistence."""

from .aiosqlite_compat import apply_aiosqlite_compatibility_patch

apply_aiosqlite_compatibility_patch()
