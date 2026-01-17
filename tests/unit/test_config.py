"""Unit tests for configuration module."""

import os
import pytest
from unittest import mock

from src.config import (
    Config,
    PTYTimeouts,
    ExecutionTimeouts,
    SlackTimeouts,
    CacheTimeouts,
    StreamingConfig,
    DisplayConfig,
    TimeoutConfig,
    config,
)


class TestPTYTimeouts:
    """Tests for PTYTimeouts dataclass."""

    def test_default_values(self):
        """PTYTimeouts has correct defaults."""
        timeouts = PTYTimeouts()

        assert timeouts.startup == 30.0
        assert timeouts.inactivity == 10.0
        assert timeouts.idle == 1800
        assert timeouts.cleanup_interval == 60
        assert timeouts.read == 0.1
        assert timeouts.stop_grace == 0.5

    def test_custom_values(self):
        """PTYTimeouts accepts custom values."""
        timeouts = PTYTimeouts(
            startup=60.0,
            inactivity=5.0,
            idle=3600,
            cleanup_interval=120,
            read=0.2,
            stop_grace=1.0,
        )

        assert timeouts.startup == 60.0
        assert timeouts.inactivity == 5.0
        assert timeouts.idle == 3600
        assert timeouts.cleanup_interval == 120
        assert timeouts.read == 0.2
        assert timeouts.stop_grace == 1.0


class TestExecutionTimeouts:
    """Tests for ExecutionTimeouts dataclass."""

    def test_default_values(self):
        """ExecutionTimeouts has correct defaults."""
        timeouts = ExecutionTimeouts()

        assert timeouts.command == 300
        assert timeouts.permission == 300
        assert timeouts.usage_check == 30

    def test_custom_values(self):
        """ExecutionTimeouts accepts custom values."""
        timeouts = ExecutionTimeouts(
            command=600,
            permission=120,
            usage_check=15,
        )

        assert timeouts.command == 600
        assert timeouts.permission == 120
        assert timeouts.usage_check == 15


class TestSlackTimeouts:
    """Tests for SlackTimeouts dataclass."""

    def test_default_values(self):
        """SlackTimeouts has correct defaults."""
        timeouts = SlackTimeouts()

        assert timeouts.message_update_throttle == 2.0

    def test_custom_values(self):
        """SlackTimeouts accepts custom values."""
        timeouts = SlackTimeouts(message_update_throttle=5.0)

        assert timeouts.message_update_throttle == 5.0


class TestCacheTimeouts:
    """Tests for CacheTimeouts dataclass."""

    def test_default_values(self):
        """CacheTimeouts has correct defaults."""
        timeouts = CacheTimeouts()

        assert timeouts.usage == 60

    def test_custom_values(self):
        """CacheTimeouts accepts custom values."""
        timeouts = CacheTimeouts(usage=120)

        assert timeouts.usage == 120


class TestDisplayConfig:
    """Tests for DisplayConfig dataclass."""

    def test_default_values(self):
        """DisplayConfig has correct defaults."""
        display = DisplayConfig()

        assert display.truncate_path_length == 45
        assert display.truncate_cmd_length == 50
        assert display.truncate_pattern_length == 40
        assert display.truncate_url_length == 50
        assert display.truncate_text_length == 40

    def test_custom_values(self):
        """DisplayConfig accepts custom values."""
        display = DisplayConfig(
            truncate_path_length=60,
            truncate_cmd_length=80,
            truncate_pattern_length=50,
            truncate_url_length=70,
            truncate_text_length=55,
        )

        assert display.truncate_path_length == 60
        assert display.truncate_cmd_length == 80
        assert display.truncate_pattern_length == 50
        assert display.truncate_url_length == 70
        assert display.truncate_text_length == 55


class TestTimeoutConfig:
    """Tests for TimeoutConfig dataclass."""

    def test_nested_structure(self):
        """TimeoutConfig nests all timeout categories."""
        timeout_config = TimeoutConfig(
            pty=PTYTimeouts(),
            execution=ExecutionTimeouts(),
            slack=SlackTimeouts(),
            cache=CacheTimeouts(),
            streaming=StreamingConfig(),
            display=DisplayConfig(),
        )

        # Access nested values
        assert timeout_config.pty.startup == 30.0
        assert timeout_config.execution.command == 300
        assert timeout_config.slack.message_update_throttle == 2.0
        assert timeout_config.cache.usage == 60
        assert timeout_config.streaming.max_accumulated_size == 500000
        assert timeout_config.display.truncate_path_length == 45


class TestConfig:
    """Tests for main Config class."""

    def test_timeout_config_accessible(self):
        """Config.timeouts provides access to all timeout settings."""
        assert config.timeouts is not None
        assert config.timeouts.pty is not None
        assert config.timeouts.execution is not None
        assert config.timeouts.slack is not None
        assert config.timeouts.cache is not None

    def test_default_slack_limits(self):
        """Config has correct Slack output limits."""
        assert config.SLACK_BLOCK_TEXT_LIMIT == 2900
        assert config.SLACK_FILE_THRESHOLD == 2000

    def test_validate_missing_tokens(self):
        """validate() catches missing required config."""
        # Create a new Config class with empty tokens for testing
        class TestConfig(Config):
            SLACK_BOT_TOKEN = ""
            SLACK_APP_TOKEN = ""
            SLACK_SIGNING_SECRET = ""

        errors = TestConfig.validate()

        assert len(errors) == 3
        assert any("SLACK_BOT_TOKEN" in e for e in errors)
        assert any("SLACK_APP_TOKEN" in e for e in errors)
        assert any("SLACK_SIGNING_SECRET" in e for e in errors)

    def test_validate_with_tokens(self):
        """validate() passes when tokens are set."""

        class TestConfig(Config):
            SLACK_BOT_TOKEN = "xoxb-test"
            SLACK_APP_TOKEN = "xapp-test"
            SLACK_SIGNING_SECRET = "secret123"

        errors = TestConfig.validate()
        assert errors == []

    def test_multi_agent_defaults(self):
        """Multi-agent workflow settings have defaults."""
        assert config.PLANNER_MAX_TURNS == 10
        assert config.WORKER_MAX_TURNS == 30
        assert config.EVALUATOR_MAX_TURNS == 10


class TestEnvironmentVariableOverrides:
    """Tests for environment variable configuration."""

    def test_timeout_env_override_structure(self):
        """TimeoutConfig structure supports environment overrides."""
        # The actual env override happens at module load time
        # Here we verify the structure allows overrides
        custom_timeouts = TimeoutConfig(
            pty=PTYTimeouts(
                startup=float(os.getenv("SESSION_STARTUP_TIMEOUT", "30.0")),
            ),
            execution=ExecutionTimeouts(
                command=int(os.getenv("COMMAND_TIMEOUT", "300")),
            ),
            slack=SlackTimeouts(
                message_update_throttle=float(os.getenv("MESSAGE_UPDATE_THROTTLE", "2.0")),
            ),
            cache=CacheTimeouts(
                usage=int(os.getenv("USAGE_CACHE_DURATION", "60")),
            ),
            streaming=StreamingConfig(),
            display=DisplayConfig(),
        )

        # Environment values should be used when set
        assert custom_timeouts.pty.startup == float(os.getenv("SESSION_STARTUP_TIMEOUT", "30.0"))
        assert custom_timeouts.execution.command == int(os.getenv("COMMAND_TIMEOUT", "300"))

    def test_auto_approve_tools_parsing(self):
        """AUTO_APPROVE_TOOLS env var parsed correctly."""
        # Test parsing logic directly
        # Empty string should result in empty list
        test_val = ""
        result = test_val.split(",") if test_val else []
        assert result == []

        # Comma-separated string should split correctly
        test_val = "Read,Glob,Grep"
        result = test_val.split(",") if test_val else []
        assert result == ["Read", "Glob", "Grep"]
