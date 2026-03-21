"""Unit tests for shared Slack response delivery helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.handlers import response_delivery


@pytest.mark.asyncio
async def test_file_response_posts_detail_button_in_channel_when_thread_missing(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )
    logger = MagicMock()
    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="very large output",
        command_id=7,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=logger,
        post_detail_button=True,
    )

    assert client.chat_postMessage.await_args.kwargs["thread_ts"] is None
    assert client.chat_postMessage.await_args.kwargs["text"] == "📋 Detailed output available"


@pytest.mark.asyncio
async def test_file_response_notifies_when_detail_button_post_fails(monkeypatch) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )
    logger = MagicMock()
    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )
    client.chat_postMessage = AsyncMock(
        side_effect=[RuntimeError("detail button failed"), {"ts": "123.456"}]
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="very large output",
        command_id=7,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=logger,
        notify_on_snippet_failure=True,
        post_detail_button=True,
    )

    assert client.chat_postMessage.await_count == 2
    assert "Could not post detailed output" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_table_followups_stay_in_channel_when_thread_missing(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: False)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_tables",
        lambda **_kwargs: [[{"type": "section"}], [{"type": "table"}]],
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="table-heavy output",
        command_id=9,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_count == 1
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] is None
