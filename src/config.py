import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Package root directory (where src/ lives)
PACKAGE_ROOT = Path(__file__).parent.parent


@dataclass
class PTYTimeouts:
    """Timeout configuration for PTY sessions."""

    startup: float = 30.0  # Max time to wait for PTY startup (30s)
    inactivity: float = 10.0  # Max inactivity before considering PTY stalled (10s)
    idle: int = 1800  # Session cleanup after 30 minutes of inactivity
    cleanup_interval: int = 60  # Run cleanup check every 60 seconds
    read: float = 0.1  # Non-blocking read timeout (100ms)
    stop_grace: float = 0.5  # Grace period for graceful shutdown (500ms)


@dataclass
class ExecutionTimeouts:
    """Timeout configuration for command execution."""

    command: int = 300  # max command runtime (5 min)
    permission: int = 300  # permission request timeout
    usage_check: int = 30  # usage CLI command timeout
    plan_approval: int = 600  # plan approval timeout (10 min)
    question_wait: int = 600  # question answer timeout (10 min) - prevent hanging on abandoned questions
    max_questions_per_conversation: int = 10  # maximum question iterations to prevent infinite loops


@dataclass
class SlackTimeouts:
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = 2.0  # Minimum 2 seconds between streaming updates (avoid rate limits)
    heartbeat_interval: float = 15.0  # Send "still working" heartbeat every 15 seconds when idle
    heartbeat_threshold: float = 20.0  # Show "still working" indicator after 20 seconds of no updates


@dataclass
class CacheTimeouts:
    """Cache duration configuration."""

    usage: int = 60  # Cache usage API checks for 60 seconds (avoid repeated API calls)


@dataclass
class StreamingConfig:
    """Configuration for streaming message updates."""

    max_accumulated_size: int = 500000  # Maximum output buffer size (500KB) before truncation
    max_tools_display: int = 10  # Maximum number of tools to show in main message (prevent UI clutter)
    tool_thread_threshold: int = 500  # Post tool output to thread if exceeds 500 characters


@dataclass
class DisplayConfig:
    """Configuration for tool activity display truncation."""

    truncate_path_length: int = 45  # Max length for file paths in tool summaries
    truncate_cmd_length: int = 50  # Max length for bash commands in tool summaries
    truncate_pattern_length: int = 40  # Max length for glob/grep patterns in tool summaries
    truncate_url_length: int = 50  # Max length for URLs in tool summaries
    truncate_text_length: int = 40  # Max length for generic text (descriptions, queries)


@dataclass
class TimeoutConfig:
    """Centralized timeout configuration."""

    pty: PTYTimeouts
    execution: ExecutionTimeouts
    slack: SlackTimeouts
    cache: CacheTimeouts
    streaming: StreamingConfig
    display: DisplayConfig


class Config:
    # Slack configuration
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN: str = os.getenv("SLACK_APP_TOKEN", "")
    SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")

    # Database - use absolute path based on package location
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(PACKAGE_ROOT / "data" / "slack_claude.db"))
    DEFAULT_WORKING_DIR: str = os.getenv("DEFAULT_WORKING_DIR", os.getcwd())

    # Claude Code configuration
    CLAUDE_PERMISSION_MODE: str = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
    DEFAULT_MODEL: Optional[str] = os.getenv("DEFAULT_MODEL")  # opus, sonnet, or haiku

    # Slack API limits (block text limit is 3000 chars per Slack API docs)
    SLACK_BLOCK_TEXT_LIMIT: int = 2900  # Slack max is 3000, use 2900 to leave room for formatting/metadata
    SLACK_FILE_THRESHOLD: int = 2000  # Attach as file if output exceeds 2000 chars (better UX for large outputs)

    # Valid permission modes for Claude Code CLI
    VALID_PERMISSION_MODES: tuple[str, ...] = (
        "acceptEdits",
        "bypassPermissions",
        "default",
        "delegate",
        "dontAsk",
        "plan",
    )

    # Multi-agent workflow turn limits (prevent infinite loops)
    PLANNER_MAX_TURNS: int = int(os.getenv("PLANNER_MAX_TURNS", "10"))  # Max planning iterations
    WORKER_MAX_TURNS: int = int(os.getenv("WORKER_MAX_TURNS", "30"))  # Max execution iterations
    EVALUATOR_MAX_TURNS: int = int(os.getenv("EVALUATOR_MAX_TURNS", "10"))  # Max evaluation iterations

    # Permissions
    AUTO_APPROVE_TOOLS: list[str] = (
        os.getenv("AUTO_APPROVE_TOOLS", "").split(",") if os.getenv("AUTO_APPROVE_TOOLS") else []
    )
    ALLOWED_TOOLS: Optional[str] = os.getenv("ALLOWED_TOOLS")  # e.g., "Read,Glob,Grep,Bash(git:*)"

    # File upload configuration (limits to prevent abuse and disk exhaustion)
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "10"))  # Max single file size (10 MB)
    MAX_UPLOAD_STORAGE_MB: int = int(os.getenv("MAX_UPLOAD_STORAGE_MB", "100"))  # Max total upload storage (100 MB)

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
            max_questions_per_conversation=int(os.getenv("MAX_QUESTIONS_PER_CONVERSATION", "10")),
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
        display=DisplayConfig(
            truncate_path_length=int(os.getenv("TRUNCATE_PATH_LENGTH", "45")),
            truncate_cmd_length=int(os.getenv("TRUNCATE_CMD_LENGTH", "50")),
            truncate_pattern_length=int(os.getenv("TRUNCATE_PATTERN_LENGTH", "40")),
            truncate_url_length=int(os.getenv("TRUNCATE_URL_LENGTH", "50")),
            truncate_text_length=int(os.getenv("TRUNCATE_TEXT_LENGTH", "40")),
        ),
    )

    # GitHub repository for web viewer links (e.g., "owner/repo")
    GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")

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
