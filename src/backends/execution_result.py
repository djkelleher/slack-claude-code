"""Shared execution result models for subprocess-backed backends."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BackendExecutionResult:
    """Common execution result fields across Claude and Codex backends."""

    success: bool
    output: str
    detailed_output: str = ""  # Full output with tool use details
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False
    git_tool_events: list[dict[str, Any]] = field(default_factory=list)
    git_diff_summary: Optional[str] = None
    git_diff_output: Optional[str] = None
