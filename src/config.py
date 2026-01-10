import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Package root directory (where src/ lives)
PACKAGE_ROOT = Path(__file__).parent.parent


@dataclass
class PTYTimeouts:
    """Timeout configuration for PTY sessions."""

    startup: float = 30.0
    inactivity: float = 10.0
    idle: int = 1800  # seconds before session cleanup
    cleanup_interval: int = 60  # seconds between cleanup checks
    read: float = 0.1  # non-blocking read timeout
    stop_grace: float = 0.5  # grace period during shutdown


@dataclass
class ExecutionTimeouts:
    """Timeout configuration for command execution."""

    command: int = 300  # max command runtime (5 min)
    permission: int = 300  # permission request timeout
    usage_check: int = 30  # usage CLI command timeout
    plan_approval: int = 600  # plan approval timeout (10 min)


@dataclass
class SlackTimeouts:
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = 2.0  # min seconds between streaming updates


@dataclass
class CacheTimeouts:
    """Cache duration configuration."""

    usage: int = 60  # usage check cache duration


@dataclass
class StreamingConfig:
    """Configuration for streaming message updates."""

    max_accumulated_size: int = 500000  # Maximum output buffer (500KB)
    max_tools_display: int = 10  # Max tools shown in main message
    tool_thread_threshold: int = 500  # Post tool output to thread if > this chars


@dataclass
class TimeoutConfig:
    """Centralized timeout configuration."""

    pty: PTYTimeouts
    execution: ExecutionTimeouts
    slack: SlackTimeouts
    cache: CacheTimeouts
    streaming: StreamingConfig


class Config:
    # Slack configuration
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")
    SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")

    # Database - use absolute path based on package location
    DATABASE_PATH: str = os.getenv(
        "DATABASE_PATH", str(PACKAGE_ROOT / "data" / "slack_claude.db")
    )
    DEFAULT_WORKING_DIR: str = os.getenv("DEFAULT_WORKING_DIR", os.getcwd())

    # Claude Code configuration
    CLAUDE_PERMISSION_MODE: str = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")

    # Slack API limits (block text limit is 3000 chars)
    SLACK_BLOCK_TEXT_LIMIT: int = 2900  # Leave room for formatting
    SLACK_FILE_THRESHOLD: int = 2000  # Attach as file if output exceeds this

    # Valid permission modes for Claude Code CLI
    VALID_PERMISSION_MODES: tuple[str, ...] = (
        "acceptEdits", "bypassPermissions", "default", "delegate", "dontAsk", "plan"
    )

    # Multi-agent workflow
    PLANNER_MAX_TURNS: int = int(os.getenv("PLANNER_MAX_TURNS", "10"))
    WORKER_MAX_TURNS: int = int(os.getenv("WORKER_MAX_TURNS", "30"))
    EVALUATOR_MAX_TURNS: int = int(os.getenv("EVALUATOR_MAX_TURNS", "10"))

    # Usage budgeting
    USAGE_THRESHOLD_DAY: float = float(os.getenv("USAGE_THRESHOLD_DAY", "85.0"))
    USAGE_THRESHOLD_NIGHT: float = float(os.getenv("USAGE_THRESHOLD_NIGHT", "95.0"))
    NIGHT_START_HOUR: int = int(os.getenv("NIGHT_START_HOUR", "22"))
    NIGHT_END_HOUR: int = int(os.getenv("NIGHT_END_HOUR", "6"))

    # Permissions
    AUTO_APPROVE_TOOLS: list[str] = (
        os.getenv("AUTO_APPROVE_TOOLS", "").split(",")
        if os.getenv("AUTO_APPROVE_TOOLS")
        else []
    )

    # File upload configuration
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
    MAX_UPLOAD_STORAGE_MB: int = int(os.getenv("MAX_UPLOAD_STORAGE_MB", "100"))

    # Centralized timeout configuration
    timeouts: TimeoutConfig = TimeoutConfig(
        pty=PTYTimeouts(
            startup=float(os.getenv("SESSION_STARTUP_TIMEOUT", "30.0")),
            inactivity=float(os.getenv("SESSION_INACTIVITY_TIMEOUT", "10.0")),
            idle=int(os.getenv("SESSION_IDLE_TIMEOUT", "1800")),
            cleanup_interval=int(os.getenv("SESSION_CLEANUP_INTERVAL", "60")),
        ),
        execution=ExecutionTimeouts(
            command=int(os.getenv("COMMAND_TIMEOUT", "300")),
            permission=int(os.getenv("PERMISSION_TIMEOUT", "300")),
            usage_check=int(os.getenv("USAGE_CHECK_TIMEOUT", "30")),
            plan_approval=int(os.getenv("PLAN_APPROVAL_TIMEOUT", "600")),
        ),
        slack=SlackTimeouts(
            message_update_throttle=float(os.getenv("MESSAGE_UPDATE_THROTTLE", "2.0")),
        ),
        cache=CacheTimeouts(
            usage=int(os.getenv("USAGE_CACHE_DURATION", "60")),
        ),
        streaming=StreamingConfig(
            max_accumulated_size=int(os.getenv("MAX_ACCUMULATED_SIZE", "500000")),
            max_tools_display=int(os.getenv("MAX_TOOLS_DISPLAY", "10")),
            tool_thread_threshold=int(os.getenv("TOOL_THREAD_THRESHOLD", "500")),
        ),
    )

    @classmethod
    def validate(cls) -> list[str]:
        """Validate required configuration."""
        errors = []
        if not cls.SLACK_BOT_TOKEN:
            errors.append("SLACK_BOT_TOKEN is required")
        if not cls.SLACK_APP_TOKEN:
            errors.append("SLACK_APP_TOKEN is required (for Socket Mode)")
        if not cls.SLACK_SIGNING_SECRET:
            errors.append("SLACK_SIGNING_SECRET is required")
        return errors


config = Config()
