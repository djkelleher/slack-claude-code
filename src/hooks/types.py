"""Hook event types and data structures."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class HookEventType(Enum):
    """Types of hook events."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    APPROVAL_NEEDED = "approval_needed"
    APPROVAL_RESPONSE = "approval_response"
    RESULT = "result"
    ERROR = "error"
    COST_UPDATE = "cost_update"
    NOTIFICATION = "notification"


@dataclass
class HookContext:
    """Context passed to hook handlers."""

    session_id: str
    channel_id: str
    thread_ts: Optional[str] = None
    user_id: Optional[str] = None
    working_directory: Optional[str] = None


@dataclass
class HookEvent:
    """An event that can trigger hooks."""

    event_type: HookEventType
    context: HookContext
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def session_id(self) -> str:
        return self.context.session_id

    @property
    def channel_id(self) -> str:
        return self.context.channel_id


@dataclass
class HookResult:
    """Result from a hook handler."""

    success: bool
    handler_name: str
    result: Any = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
