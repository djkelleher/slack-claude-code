"""Shared execution result models for subprocess-backed backends."""

from dataclasses import dataclass
from typing import Optional


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
