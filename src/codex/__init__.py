"""Codex CLI execution layer."""

from .streaming import StreamMessage, StreamParser, ToolActivity
from .subprocess_executor import ExecutionResult, SubprocessExecutor

__all__ = [
    "ExecutionResult",
    "SubprocessExecutor",
    "PTYExecutor",
    "StreamMessage",
    "StreamParser",
    "ToolActivity",
]


def __getattr__(name: str):
    """Lazy-load PTYExecutor to avoid circular imports during module init."""
    if name == "PTYExecutor":
        from .pty_executor import PTYExecutor

        return PTYExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
