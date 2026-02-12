"""PTY session types and dataclasses."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SessionState(Enum):
    """State of a PTY session."""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class PTYSessionConfig:
    """Configuration for a PTY session."""

    working_directory: str = "~"
    sandbox_mode: str = "workspace-write"
    approval_mode: str = "on-request"
    model: Optional[str] = None

    # Timeouts
    startup_timeout: float = 30.0
    inactivity_timeout: float = 10.0
    read_timeout: float = 0.1
    idle_timeout: float = 1800.0  # 30 minutes
    stop_grace_period: float = 0.5

    # Terminal dimensions
    cols: int = 120
    rows: int = 40

    # Additional CLI arguments
    codex_args: list[str] = field(default_factory=list)
