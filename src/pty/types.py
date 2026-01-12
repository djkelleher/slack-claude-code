"""PTY session types and dataclasses."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..config import config
from .parser import OutputType


class SessionState(Enum):
    """State of a PTY session."""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    AWAITING_APPROVAL = "awaiting_approval"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class PTYSessionConfig:
    """Configuration for a PTY session.

    Defaults are pulled from centralized config.timeouts.pty.
    """

    working_directory: str = "~"
    inactivity_timeout: float = field(default_factory=lambda: config.timeouts.pty.inactivity)
    read_timeout: float = field(default_factory=lambda: config.timeouts.pty.read)
    startup_timeout: float = field(default_factory=lambda: config.timeouts.pty.startup)
    cols: int = 120
    rows: int = 40
    claude_args: list[str] = field(default_factory=list)


@dataclass
class ResponseChunk:
    """A chunk of response from Claude.

    Uses OutputType from parser module for output_type field.
    """

    content: str
    output_type: Optional[OutputType] = None
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    is_final: bool = False
    is_permission_request: bool = False
    raw: str = ""

    def __post_init__(self):
        if self.output_type is None:
            self.output_type = OutputType.TEXT


@dataclass
class SessionResponse:
    """Complete response from a session prompt."""

    output: str
    success: bool = True
    error: Optional[str] = None
    was_permission_request: bool = False
