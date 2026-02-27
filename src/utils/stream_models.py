"""Shared stream message models and helpers for Claude/Codex backends."""

from dataclasses import dataclass
from typing import ClassVar, Optional

from src.config import config
from src.utils.tool_input_summary import format_tool_input_summary


def concat_with_spacing(existing: str, new: str) -> str:
    """Concatenate text chunks while preserving readable separation."""
    if not existing or not new:
        return existing + new
    if existing[-1] in ("\n", " ") or new[0] in ("\n", " "):
        return existing + new
    return existing + "\n\n" + new


@dataclass
class BaseToolActivity:
    """Structured representation of a tool invocation."""

    SUMMARY_RULES: ClassVar[dict[str, dict]] = {}

    id: str
    name: str
    input: dict
    input_summary: str
    result: Optional[str] = None
    full_result: Optional[str] = None
    is_error: bool = False
    duration_ms: Optional[int] = None
    started_at: Optional[float] = None
    timestamp: Optional[float] = None

    @classmethod
    def create_input_summary(cls, name: str, input_dict: dict) -> str:
        """Create a short summary of tool input for inline display."""
        display = config.timeouts.display
        return format_tool_input_summary(name, input_dict, display, cls.SUMMARY_RULES)


@dataclass
class StreamMessage:
    """Parsed message from normalized backend stream output."""

    type: str
    content: str = ""
    detailed_content: str = ""
    tool_activities: Optional[list[BaseToolActivity]] = None
    session_id: Optional[str] = None
    is_final: bool = False
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    raw: dict = None

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}
        if self.tool_activities is None:
            self.tool_activities = []
