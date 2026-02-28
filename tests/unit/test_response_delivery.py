"""Unit tests for shared Slack response delivery helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.handlers import response_delivery


@pytest.mark.asyncio
async def test_file_response_posts_snippet_in_processing_thread_when_thread_missing(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )
    logger = MagicMock()
    snippet_mock = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )
    monkeypatch.setattr(response_delivery, "post_text_snippet", snippet_mock)

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
    )

    assert snippet_mock.await_args.kwargs["thread_ts"] == "111.222"


@pytest.mark.asyncio
async def test_file_response_notifies_when_snippet_post_fails(monkeypatch) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
    )
    logger = MagicMock()
    snippet_mock = AsyncMock(side_effect=RuntimeError("snippet failed"))

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )
    monkeypatch.setattr(response_delivery, "post_text_snippet", snippet_mock)

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
    )

    assert client.chat_postMessage.await_count == 1
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "111.222"
    assert (
        "Could not post detailed output"
        in client.chat_postMessage.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_table_followups_use_processing_thread_when_thread_missing(
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
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] == "111.222"
