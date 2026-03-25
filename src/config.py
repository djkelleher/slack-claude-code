import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_storage import get_storage

# Global constant for application plan directory
PLANS_DIR = str(Path.home() / ".slack-claude-code" / "plans")


@dataclass(frozen=True)
class ModelOption:
    """Static metadata for a selectable model option."""

    name: str
    value: Optional[str]
    display: str
    desc: str


CLAUDE_MODEL_DISPLAY: dict[str | None, str] = {
    None: "Opus 4.6",
    "default": "Opus 4.6",
    "opus": "Opus 4.6",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-6[1m]": "Opus 4.6 (1M context)",
    "claude-opus-4-5": "Opus 4.5",
    "sonnet": "Sonnet 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-6[1m]": "Sonnet 4.6 (1M context)",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "haiku": "Haiku 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
}

CLAUDE_MODEL_ALIASES: dict[str, str | None] = {
    "default": None,
    "default (recommended)": None,
    "recommended": None,
    "opus": None,
    "opus-4.6": None,
    "claude-opus-4-6": None,
    "opus-4.5": "claude-opus-4-5",
    "claude-opus-4-5": "claude-opus-4-5",
    "opus-1m": "claude-opus-4-6[1m]",
    "opus (1m context)": "claude-opus-4-6[1m]",
    "claude-opus-4-6[1m]": "claude-opus-4-6[1m]",
    "sonnet": "sonnet",
    "sonnet-4.6": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "sonnet-4.5": "claude-sonnet-4-5",
    "claude-sonnet-4-5": "claude-sonnet-4-5",
    "sonnet-1m": "claude-sonnet-4-6[1m]",
    "sonnet (1m context)": "claude-sonnet-4-6[1m]",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6[1m]",
    "haiku": "haiku",
    "haiku-4.5": "haiku",
    "claude-haiku-4-5": "haiku",
    "co45m": "claude-opus-4-5-medium",
    "co46h": "claude-opus-4-6-high",
}

CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex": "gpt-5.3-codex",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
    "g34h": "gpt-3.4-high",
}

CLAUDE_DEFAULT_ALIASES: set[str] = {"default", "opus", "opus-4.6", "claude-opus-4-6"}

CLAUDE_MODEL_OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(
        name="opus-4-6",
        value=None,
        display="Opus 4.6",
        desc="Opus 4.6 · Most capable for complex work",
    ),
    ModelOption(
        name="sonnet",
        value="sonnet",
        display="Sonnet 4.6",
        desc="Sonnet 4.6 · Best for everyday tasks",
    ),
    ModelOption(
        name="haiku",
        value="haiku",
        display="Haiku 4.5",
        desc="Haiku 4.5 · Fastest for quick answers",
    ),
    ModelOption(
        name="opus-4-5",
        value="claude-opus-4-5",
        display="Opus 4.5",
        desc="Previous Opus generation (legacy model ID support)",
    ),
    ModelOption(
        name="sonnet-4-5",
        value="claude-sonnet-4-5",
        display="Sonnet 4.5",
        desc="Previous Sonnet generation (legacy model ID support)",
    ),
)

CODEX_BASE_MODEL_OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(
        name="gpt-5.3-codex",
        value="gpt-5.3-codex",
        display="GPT-5.3 Codex",
        desc="Latest frontier agentic coding model",
    ),
    ModelOption(
        name="gpt-5.4",
        value="gpt-5.4",
        display="GPT-5.4",
        desc="Latest frontier agentic coding model",
    ),
    ModelOption(
        name="gpt-5.3-codex-spark",
        value="gpt-5.3-codex-spark",
        display="GPT-5.3 Codex Spark",
        desc="Ultra-fast coding model",
    ),
    ModelOption(
        name="gpt-5.2-codex",
        value="gpt-5.2-codex",
        display="GPT-5.2 Codex",
        desc="Frontier agentic coding model",
    ),
    ModelOption(
        name="gpt-5.1-codex-max",
        value="gpt-5.1-codex-max",
        display="GPT-5.1 Codex Max",
        desc="Codex-optimized flagship for deep and fast reasoning",
    ),
    ModelOption(
        name="gpt-5.2",
        value="gpt-5.2",
        display="GPT-5.2",
        desc="Latest frontier model with improvements across knowledge, reasoning and coding",
    ),
    ModelOption(
        name="gpt-5.1-codex-mini",
        value="gpt-5.1-codex-mini",
        display="GPT-5.1 Codex Mini",
        desc="Optimized for codex. Cheaper, faster, but less capable",
    ),
)

CODEX_EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra-High",
}

CLAUDE_MODELS: set[str] = {
    alias for alias, value in CLAUDE_MODEL_ALIASES.items() if alias and value != "sonnet"
}
CLAUDE_MODELS.update(option.value for option in CLAUDE_MODEL_OPTIONS if option.value is not None)
CLAUDE_MODELS.update({"sonnet", "haiku"})

CODEX_MODELS: set[str] = {
    option.value for option in CODEX_BASE_MODEL_OPTIONS if option.value is not None
}


EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max", "auto")


def parse_model_effort(model: str) -> tuple[str, Optional[str]]:
    """Parse effort suffix from a Codex model name.

    Parameters
    ----------
    model : str
        Model name, possibly with effort suffix
        (e.g., "gpt-5.3-codex-high", "gpt-5.3-codex-extra-high").

    Returns
    -------
    tuple[str, Optional[str]]
        (base_model, effort_level) — effort_level is None if no suffix found.
    """
    model_clean = model.strip()
    model_lower = model_clean.lower()
    suffix_map = {
        "-extra-high": "xhigh",
        "-extra_high": "xhigh",
        "-extrahigh": "xhigh",
        "-xhigh": "xhigh",
        "-medium": "medium",
        "-high": "high",
        "-low": "low",
    }
    for suffix, level in suffix_map.items():
        if model_lower.endswith(suffix):
            return model_clean[: -len(suffix)], level
    return model_clean, None


def parse_claude_model_effort(model: str) -> tuple[str, Optional[str]]:
    """Parse Claude effort suffix embedded in a model string.

    Examples
    --------
    ``claude-opus-4-6-high`` -> (``claude-opus-4-6``, ``high``)
    ``claude-opus-4-6-max`` -> (``claude-opus-4-6``, ``max``)
    """
    model_clean = model.strip()
    model_lower = model_clean.lower()
    suffix_map = {
        "-medium": "medium",
        "-high": "high",
        "-low": "low",
        "-max": "max",
        "-auto": "auto",
    }
    for suffix, level in suffix_map.items():
        if model_lower.endswith(suffix):
            return model_clean[: -len(suffix)], level
    return model_clean, None


def is_supported_codex_model(model: str) -> bool:
    """Return True when model is one of the supported Codex models, with optional effort."""
    base_model, effort = parse_model_effort(model)
    if base_model.lower() not in CODEX_MODELS:
        return False
    if effort is None:
        return True
    return effort in EFFORT_LEVELS


def looks_like_codex_model(model_name: str) -> bool:
    """Best-effort classifier for codex-like model IDs."""
    normalized = (model_name or "").strip().lower()
    return normalized.startswith("gpt-") or normalized.startswith("codex")


def get_backend_for_model(model: Optional[str]) -> str:
    """
    Determine which backend to use based on the model name.

    Args:
        model: The model name (e.g., "opus", "gpt-5.3-codex")

    Returns:
        "claude" or "codex"
    """
    if model is None:
        return "claude"  # Default to Claude

    model_lower = model.lower()

    # Check exact matches first
    if model_lower in CLAUDE_MODELS:
        return "claude"
    if is_supported_codex_model(model_lower):
        return "codex"

    # Check prefixes for extended model names
    if model_lower.startswith("claude"):
        return "claude"
    if looks_like_codex_model(model_lower):
        return "codex"
    # Default to Claude for unknown models
    return "claude"


class ExecutionTimeouts(BaseModel):
    """Timeout configuration for command execution."""

    usage_check: int = 30
    max_questions_per_conversation: int = 10

    @field_validator("usage_check", "max_questions_per_conversation")
    @classmethod
    def validate_positive(cls, v: int, info) -> int:
        """Ensure timeout values are positive integers."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer, got {v}")
        return v


class SlackTimeouts(BaseModel):
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = 2.0
    heartbeat_interval: float = 15.0
    heartbeat_threshold: float = 20.0

    @field_validator("message_update_throttle", "heartbeat_interval", "heartbeat_threshold")
    @classmethod
    def validate_positive_float(cls, v: float, info) -> float:
        """Ensure timeout values are positive."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class CacheTimeouts(BaseModel):
    """Cache duration configuration."""

    usage: int = 60

    @field_validator("usage")
    @classmethod
    def validate_positive(cls, v: int, info) -> int:
        """Ensure cache duration is positive."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer, got {v}")
        return v


class StreamingConfig(BaseModel):
    """Configuration for streaming message updates."""

    max_accumulated_size: int = 500000
    max_tools_display: int = 10
    tool_thread_threshold: int = 500


class LimitsConfig(BaseModel):
    """Configuration for input/output limits."""

    max_prompt_length: int = 50000  # Maximum command input length
    max_action_value_size: int = 1024 * 1024  # Max JSON payload in actions (1MB)
    plan_file_max_age_seconds: int = 300  # Time window for plan file discovery


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
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


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
    SLACK_QUESTION_MENTION: str = ""

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
    SLACK_MAX_BLOCKS_PER_MESSAGE: int = 50

    # Valid permission modes for Claude Code CLI
    VALID_PERMISSION_MODES: tuple[str, ...] = (
        "acceptEdits",
        "bypassPermissions",
        "default",
        "delegate",
        "dontAsk",
        "plan",
    )

    # Permissions - stored as comma-separated string, converted to list via property
    AUTO_APPROVE_TOOLS_STR: str = Field(default="", alias="AUTO_APPROVE_TOOLS")
    ALLOWED_TOOLS: Optional[str] = None

    # File upload configuration
    MAX_FILE_SIZE_MB: int = 10
    MAX_UPLOAD_STORAGE_MB: int = 100

    # GitHub repository for web viewer links
    GITHUB_REPO: str = ""

    # Codex configuration
    CODEX_SANDBOX_MODE: str = "danger-full-access"
    CODEX_APPROVAL_MODE: str = "on-request"
    CODEX_PREPEND_DEFAULT_INSTRUCTIONS: bool = True
    CODEX_DEFAULT_INSTRUCTIONS_FILE: str = str(Path.home() / ".codex" / "default_instructions.txt")

    # Queue behavior
    QUEUE_AUTO_ANSWER_QUESTIONS: bool = False
    QUEUE_AUTO_APPROVE_PERMISSIONS: bool = True
    QUEUE_PAUSE_ON_QUESTIONS: bool = False

    # Valid sandbox modes for Codex app-server
    VALID_SANDBOX_MODES: tuple[str, ...] = (
        "read-only",
        "workspace-write",
        "danger-full-access",
    )

    # Valid approval modes for Codex app-server
    VALID_APPROVAL_MODES: tuple[str, ...] = (
        "untrusted",
        "on-failure",
        "on-request",
        "never",
    )

    # Execution timeout overrides from environment
    USAGE_CHECK_TIMEOUT: int = 30
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

    @functools.cached_property
    def timeouts(self) -> TimeoutConfig:
        """Build TimeoutConfig from environment variables."""
        return TimeoutConfig(
            execution=ExecutionTimeouts(
                usage_check=self.USAGE_CHECK_TIMEOUT,
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
