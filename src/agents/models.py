"""Agent data models for the configurable subagent system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AgentSource(Enum):
    """Where the agent definition comes from."""

    BUILTIN = "builtin"
    PROJECT = "project"
    USER = "user"


class AgentModelChoice(Enum):
    """Model selection for agents."""

    INHERIT = "inherit"
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


class AgentPermissionMode(Enum):
    """Permission modes for agent execution."""

    INHERIT = "inherit"
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"
    PLAN = "plan"


@dataclass
class AgentConfig:
    """Configuration for a subagent.

    Mirrors terminal Claude Code's agent definition format with YAML frontmatter.

    Parameters
    ----------
    name : str
        Unique identifier (filename without .md for file-based agents).
    description : str
        Used for agent selection - Claude decides based on this.
    source : AgentSource
        Where this agent was loaded from.
    file_path : str, optional
        Path to the .md file (None for built-ins).
    system_prompt : str
        The main prompt content (markdown body).
    tools : list[str]
        Allowlist of tools (empty = all tools allowed).
    disallowed_tools : list[str]
        Denylist of tools to remove from allowed set.
    model : AgentModelChoice
        Model to use for this agent.
    permission_mode : AgentPermissionMode
        Permission mode for execution.
    max_turns : int
        Maximum conversation turns.
    is_builtin : bool
        True for built-in agents (cannot be edited/deleted).
    """

    name: str
    description: str
    source: AgentSource
    file_path: Optional[str] = None
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: AgentModelChoice = AgentModelChoice.INHERIT
    permission_mode: AgentPermissionMode = AgentPermissionMode.INHERIT
    max_turns: int = 50
    is_builtin: bool = False


class AgentExecutionStatus(Enum):
    """Status of an agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentExecution:
    """Tracks an active agent execution."""

    execution_id: str
    agent_name: str
    channel_id: str
    task_description: str
    working_directory: str
    thread_ts: Optional[str] = None
    status: AgentExecutionStatus = AgentExecutionStatus.PENDING
    session_id: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    turn_count: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    message_ts: Optional[str] = None
    run_in_background: bool = False


@dataclass
class AgentRunResult:
    """Result of an agent execution."""

    execution_id: str
    agent_name: str
    success: bool
    output: str
    detailed_output: str = ""
    error: Optional[str] = None
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    turn_count: int = 0
