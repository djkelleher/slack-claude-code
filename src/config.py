from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_storage import get_storage


class ExecutionTimeouts(BaseModel):
    """Timeout configuration for command execution."""

    permission: int = 300
    usage_check: int = 30
    plan_approval: int = 600
    max_questions_per_conversation: int = 10


class SlackTimeouts(BaseModel):
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = 2.0
    heartbeat_interval: float = 15.0
    heartbeat_threshold: float = 20.0


class CacheTimeouts(BaseModel):
    """Cache duration configuration."""

    usage: int = 60


class StreamingConfig(BaseModel):
    """Configuration for streaming message updates."""

    max_accumulated_size: int = 500000
    max_tools_display: int = 10
    tool_thread_threshold: int = 500


class DisplayConfig(BaseModel):
    """Configuration for tool activity display truncation."""

    truncate_path_length: int = 45
    truncate_cmd_length: int = 50
    truncate_pattern_length: int = 40
    truncate_url_length: int = 50
    truncate_text_length: int = 40


class TimeoutConfig(BaseModel):
    """Centralized timeout configuration."""

    execution: ExecutionTimeouts = Field(default_factory=ExecutionTimeouts)
    slack: SlackTimeouts = Field(default_factory=SlackTimeouts)
    cache: CacheTimeouts = Field(default_factory=CacheTimeouts)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)


class EncryptedSettingsSource:
    """Settings source that reads from encrypted storage."""

    def __init__(self, settings_cls: type[BaseSettings]):
        self.settings_cls = settings_cls

    def __call__(self) -> dict[str, Any]:
        """Load settings from encrypted storage."""
        storage = get_storage()
        return storage.get_all()


class Config(BaseSettings):
    """
    Application configuration loaded from multiple sources.

    Priority (highest to lowest):
    1. Encrypted storage (~/.slack-claude-code/config.enc)
    2. Environment variables
    3. .env file
    4. Default values
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Customize settings sources to add encrypted storage with highest priority."""
        return (
            init_settings,
            EncryptedSettingsSource(settings_cls),  # Encrypted storage (highest priority)
            env_settings,  # Environment variables
            dotenv_settings,  # .env file
            file_secret_settings,
        )

    # Slack configuration
    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # Database - defaults to ~/.slack-claude-code/
    DATABASE_PATH: str = Field(
        default_factory=lambda: str(Path.home() / ".slack-claude-code" / "slack_claude.db")
    )
    DEFAULT_WORKING_DIR: str = Field(default_factory=lambda: str(Path.cwd()))

    # Claude Code configuration
    CLAUDE_PERMISSION_MODE: str = "bypassPermissions"
    DEFAULT_MODEL: Optional[str] = None

    # Default permission mode constant (used as fallback when invalid mode specified)
    DEFAULT_BYPASS_MODE: str = "bypassPermissions"

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

    # Permissions - stored as comma-separated string, converted to list via property
    AUTO_APPROVE_TOOLS_STR: str = Field(default="", alias="AUTO_APPROVE_TOOLS")
    ALLOWED_TOOLS: Optional[str] = None

    # File upload configuration
    MAX_FILE_SIZE_MB: int = 10
    MAX_UPLOAD_STORAGE_MB: int = 100

    # GitHub repository for web viewer links
    GITHUB_REPO: str = ""

    # Execution timeout overrides from environment
    PERMISSION_TIMEOUT: int = 300
    USAGE_CHECK_TIMEOUT: int = 30
    PLAN_APPROVAL_TIMEOUT: int = 600
    MAX_QUESTIONS_PER_CONVERSATION: int = 10

    # Slack timeout overrides from environment
    MESSAGE_UPDATE_THROTTLE: float = 2.0

    # Cache timeout overrides from environment
    USAGE_CACHE_DURATION: int = 60

    # Streaming config overrides from environment
    MAX_ACCUMULATED_SIZE: int = 500000
    MAX_TOOLS_DISPLAY: int = 10
    TOOL_THREAD_THRESHOLD: int = 500

    # Display config overrides from environment
    TRUNCATE_PATH_LENGTH: int = 45
    TRUNCATE_CMD_LENGTH: int = 50
    TRUNCATE_PATTERN_LENGTH: int = 40
    TRUNCATE_URL_LENGTH: int = 50
    TRUNCATE_TEXT_LENGTH: int = 40

    @property
    def AUTO_APPROVE_TOOLS(self) -> list[str]:
        """Parse AUTO_APPROVE_TOOLS from comma-separated string."""
        if not self.AUTO_APPROVE_TOOLS_STR:
            return []
        return [t.strip() for t in self.AUTO_APPROVE_TOOLS_STR.split(",") if t.strip()]

    @property
    def timeouts(self) -> TimeoutConfig:
        """Build TimeoutConfig from environment variables."""
        return TimeoutConfig(
            execution=ExecutionTimeouts(
                permission=self.PERMISSION_TIMEOUT,
                usage_check=self.USAGE_CHECK_TIMEOUT,
                plan_approval=self.PLAN_APPROVAL_TIMEOUT,
                max_questions_per_conversation=self.MAX_QUESTIONS_PER_CONVERSATION,
            ),
            slack=SlackTimeouts(
                message_update_throttle=self.MESSAGE_UPDATE_THROTTLE,
            ),
            cache=CacheTimeouts(
                usage=self.USAGE_CACHE_DURATION,
            ),
            streaming=StreamingConfig(
                max_accumulated_size=self.MAX_ACCUMULATED_SIZE,
                max_tools_display=self.MAX_TOOLS_DISPLAY,
                tool_thread_threshold=self.TOOL_THREAD_THRESHOLD,
            ),
            display=DisplayConfig(
                truncate_path_length=self.TRUNCATE_PATH_LENGTH,
                truncate_cmd_length=self.TRUNCATE_CMD_LENGTH,
                truncate_pattern_length=self.TRUNCATE_PATTERN_LENGTH,
                truncate_url_length=self.TRUNCATE_URL_LENGTH,
                truncate_text_length=self.TRUNCATE_TEXT_LENGTH,
            ),
        )

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
