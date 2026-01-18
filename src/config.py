from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PTYTimeouts(BaseSettings):
    """Timeout configuration for PTY sessions."""

    model_config = SettingsConfigDict(env_prefix="SESSION_")

    startup: float = Field(default=30.0, alias="SESSION_STARTUP_TIMEOUT")
    inactivity: float = Field(default=10.0, alias="SESSION_INACTIVITY_TIMEOUT")
    idle: int = Field(default=1800, alias="SESSION_IDLE_TIMEOUT")
    cleanup_interval: int = Field(default=60, alias="SESSION_CLEANUP_INTERVAL")
    read: float = 0.1  # Non-blocking read timeout (100ms)
    stop_grace: float = 0.5  # Grace period for graceful shutdown (500ms)


class ExecutionTimeouts(BaseSettings):
    """Timeout configuration for command execution."""

    permission: int = Field(default=300, alias="PERMISSION_TIMEOUT")
    usage_check: int = Field(default=30, alias="USAGE_CHECK_TIMEOUT")
    plan_approval: int = Field(default=600, alias="PLAN_APPROVAL_TIMEOUT")
    question_wait: int = 600  # Question answer timeout (10 min)
    max_questions_per_conversation: int = Field(
        default=10, alias="MAX_QUESTIONS_PER_CONVERSATION"
    )


class SlackTimeouts(BaseSettings):
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = Field(default=2.0, alias="MESSAGE_UPDATE_THROTTLE")
    heartbeat_interval: float = 15.0
    heartbeat_threshold: float = 20.0


class CacheTimeouts(BaseSettings):
    """Cache duration configuration."""

    usage: int = Field(default=60, alias="USAGE_CACHE_DURATION")


class StreamingConfig(BaseSettings):
    """Configuration for streaming message updates."""

    max_accumulated_size: int = Field(default=500000, alias="MAX_ACCUMULATED_SIZE")
    max_tools_display: int = Field(default=10, alias="MAX_TOOLS_DISPLAY")
    tool_thread_threshold: int = Field(default=500, alias="TOOL_THREAD_THRESHOLD")


class DisplayConfig(BaseSettings):
    """Configuration for tool activity display truncation."""

    truncate_path_length: int = Field(default=45, alias="TRUNCATE_PATH_LENGTH")
    truncate_cmd_length: int = Field(default=50, alias="TRUNCATE_CMD_LENGTH")
    truncate_pattern_length: int = Field(default=40, alias="TRUNCATE_PATTERN_LENGTH")
    truncate_url_length: int = Field(default=50, alias="TRUNCATE_URL_LENGTH")
    truncate_text_length: int = Field(default=40, alias="TRUNCATE_TEXT_LENGTH")


class TimeoutConfig(BaseSettings):
    """Centralized timeout configuration."""

    pty: PTYTimeouts = Field(default_factory=PTYTimeouts)
    execution: ExecutionTimeouts = Field(default_factory=ExecutionTimeouts)
    slack: SlackTimeouts = Field(default_factory=SlackTimeouts)
    cache: CacheTimeouts = Field(default_factory=CacheTimeouts)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)


class Config(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Slack configuration
    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # Database - defaults to current working directory
    DATABASE_PATH: str = Field(default_factory=lambda: str(Path.cwd() / "slack_claude.db"))
    DEFAULT_WORKING_DIR: str = Field(default_factory=lambda: str(Path.cwd()))

    # Claude Code configuration
    CLAUDE_PERMISSION_MODE: str = "bypassPermissions"
    DEFAULT_MODEL: Optional[str] = None

    # Slack API limits
    SLACK_BLOCK_TEXT_LIMIT: int = 2900
    SLACK_FILE_THRESHOLD: int = 2000

    # Valid permission modes for Claude Code CLI
    VALID_PERMISSION_MODES: tuple[str, ...] = (
        "acceptEdits",
        "bypassPermissions",
        "default",
        "delegate",
        "dontAsk",
        "plan",
    )

    # Multi-agent workflow turn limits
    PLANNER_MAX_TURNS: int = 10
    WORKER_MAX_TURNS: int = 30
    EVALUATOR_MAX_TURNS: int = 10

    # Permissions
    AUTO_APPROVE_TOOLS: list[str] = Field(default_factory=list)
    ALLOWED_TOOLS: Optional[str] = None

    # File upload configuration
    MAX_FILE_SIZE_MB: int = 10
    MAX_UPLOAD_STORAGE_MB: int = 100

    # GitHub repository for web viewer links
    GITHUB_REPO: str = ""

    # Centralized timeout configuration
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)

    @field_validator("AUTO_APPROVE_TOOLS", mode="before")
    @classmethod
    def parse_auto_approve_tools(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    def validate_required(self) -> list[str]:
        """Validate required configuration."""
        errors = []
        if not self.SLACK_BOT_TOKEN:
            errors.append("SLACK_BOT_TOKEN is required")
        if not self.SLACK_APP_TOKEN:
            errors.append("SLACK_APP_TOKEN is required (for Socket Mode)")
        if not self.SLACK_SIGNING_SECRET:
            errors.append("SLACK_SIGNING_SECRET is required")
        return errors


config = Config()
