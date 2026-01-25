"""Unit tests for configuration module."""

import pytest

from src.config import (
    Config,
    ExecutionTimeouts,
    SlackTimeouts,
    CacheTimeouts,
    StreamingConfig,
    DisplayConfig,
    TimeoutConfig,
    config,
)


class TestExecutionTimeouts:
    """Tests for ExecutionTimeouts settings."""

    def test_default_values(self):
        """ExecutionTimeouts has correct defaults."""
        timeouts = ExecutionTimeouts()

        assert timeouts.permission == 300
        assert timeouts.usage_check == 30
        assert timeouts.max_questions_per_conversation == 10

    def test_custom_values(self):
        """ExecutionTimeouts accepts custom values."""
        timeouts = ExecutionTimeouts(
            permission=120,
            usage_check=15,
            max_questions_per_conversation=5,
        )

        assert timeouts.permission == 120
        assert timeouts.usage_check == 15
        assert timeouts.max_questions_per_conversation == 5


class TestSlackTimeouts:
    """Tests for SlackTimeouts settings."""

    def test_default_values(self):
        """SlackTimeouts has correct defaults."""
        timeouts = SlackTimeouts()

        assert timeouts.message_update_throttle == 2.0

    def test_custom_values(self):
        """SlackTimeouts accepts custom values."""
        timeouts = SlackTimeouts(message_update_throttle=5.0)

        assert timeouts.message_update_throttle == 5.0


class TestCacheTimeouts:
    """Tests for CacheTimeouts settings."""

    def test_default_values(self):
        """CacheTimeouts has correct defaults."""
        timeouts = CacheTimeouts()

        assert timeouts.usage == 60

    def test_custom_values(self):
        """CacheTimeouts accepts custom values."""
        timeouts = CacheTimeouts(usage=120)

        assert timeouts.usage == 120


class TestDisplayConfig:
    """Tests for DisplayConfig settings."""

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
    """Tests for TimeoutConfig settings."""

    def test_nested_structure(self):
        """TimeoutConfig nests all timeout categories."""
        timeout_config = TimeoutConfig()

        # Access nested values
        assert timeout_config.execution.permission == 300
        assert timeout_config.slack.message_update_throttle == 2.0
        assert timeout_config.cache.usage == 60
        assert timeout_config.streaming.max_accumulated_size == 500000
        assert timeout_config.display.truncate_path_length == 45


class TestConfig:
    """Tests for main Config class."""

    def test_timeout_config_accessible(self):
        """Config.timeouts provides access to all timeout settings."""
        assert config.timeouts is not None
        assert config.timeouts.execution is not None
        assert config.timeouts.slack is not None
        assert config.timeouts.cache is not None

    def test_default_slack_limits(self):
        """Config has correct Slack output limits."""
        assert config.SLACK_BLOCK_TEXT_LIMIT == 2900
        assert config.SLACK_FILE_THRESHOLD == 2000

    def test_validate_missing_tokens(self):
        """validate_required() catches missing required config."""
        test_config = Config(
            SLACK_BOT_TOKEN="",
            SLACK_APP_TOKEN="",
            SLACK_SIGNING_SECRET="",
        )

        errors = test_config.validate_required()

        assert len(errors) == 3
        assert any("SLACK_BOT_TOKEN" in e for e in errors)
        assert any("SLACK_APP_TOKEN" in e for e in errors)
        assert any("SLACK_SIGNING_SECRET" in e for e in errors)

    def test_validate_with_tokens(self):
        """validate_required() passes when tokens are set."""
        test_config = Config(
            SLACK_BOT_TOKEN="xoxb-test",
            SLACK_APP_TOKEN="xapp-test",
            SLACK_SIGNING_SECRET="secret123",
        )

        errors = test_config.validate_required()
        assert errors == []

class TestAutoApproveToolsParsing:
    """Tests for AUTO_APPROVE_TOOLS parsing."""

    def test_empty_string(self, monkeypatch):
        """Empty string results in empty list."""
        monkeypatch.delenv("AUTO_APPROVE_TOOLS", raising=False)
        test_config = Config(AUTO_APPROVE_TOOLS_STR="", _env_file=None)
        assert test_config.AUTO_APPROVE_TOOLS == []

    def test_comma_separated_string(self, monkeypatch):
        """Comma-separated string parses correctly."""
        monkeypatch.delenv("AUTO_APPROVE_TOOLS", raising=False)
        test_config = Config(AUTO_APPROVE_TOOLS_STR="Read,Glob,Grep", _env_file=None)
        assert test_config.AUTO_APPROVE_TOOLS == ["Read", "Glob", "Grep"]

    def test_whitespace_handling(self, monkeypatch):
        """Whitespace around values is stripped."""
        monkeypatch.delenv("AUTO_APPROVE_TOOLS", raising=False)
        test_config = Config(AUTO_APPROVE_TOOLS_STR=" Read , Glob , Grep ", _env_file=None)
        assert test_config.AUTO_APPROVE_TOOLS == ["Read", "Glob", "Grep"]
