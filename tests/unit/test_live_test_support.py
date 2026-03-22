"""Unit tests for live test support helpers."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from tests import conftest as test_conftest
from tests.integration import test_slack_app_live


def test_load_dotenv_populates_missing_values(tmp_path, monkeypatch) -> None:
    """Dotenv loader should populate missing keys and ignore comments."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "# comment",
                "SLACK_BOT_TOKEN=xoxb-test",
                "export SLACK_TEST_CHANNEL=C123",
                "QUOTED='hello world'",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_TEST_CHANNEL", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    test_conftest._load_dotenv(str(dotenv_path))

    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-test"
    assert os.environ["SLACK_TEST_CHANNEL"] == "C123"
    assert os.environ["QUOTED"] == "hello world"


def test_load_dotenv_does_not_override_existing_values(tmp_path, monkeypatch) -> None:
    """Existing environment values should win over .env defaults."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("SLACK_BOT_TOKEN=xoxb-from-file\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-existing")

    test_conftest._load_dotenv(str(dotenv_path))

    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-existing"


@pytest.mark.asyncio
async def test_post_user_message_or_skip_returns_post_response() -> None:
    """Successful post should pass the Slack response through unchanged."""
    client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ok": True, "ts": "123.456"}))

    response = await test_slack_app_live._post_user_message_or_skip(
        client,
        channel="C123",
        text="hello",
    )

    assert response == {"ok": True, "ts": "123.456"}


@pytest.mark.asyncio
async def test_post_user_message_or_skip_skips_for_missing_scope() -> None:
    """Missing Slack scopes should become a pytest skip with clear details."""

    class FakeResponse(dict):
        """Minimal Slack response stub for SlackApiError."""

        status_code = 200

    response = FakeResponse(
        ok=False,
        error="missing_scope",
        needed="chat:write:bot",
        provided="identify",
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(
            side_effect=SlackApiError(message="scope error", response=response)
        )
    )

    with pytest.raises(pytest.skip.Exception, match="needed=chat:write:bot"):
        await test_slack_app_live._post_user_message_or_skip(
            client,
            channel="C123",
            text="hello",
        )
