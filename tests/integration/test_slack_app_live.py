"""Live end-to-end tests for Slack app event handlers.

These tests validate that the running Slack app receives real events and responds.

Required environment variables:
- SLACK_BOT_TOKEN
- SLACK_USER_TOKEN
- SLACK_TEST_CHANNEL

Run with: pytest tests/integration/test_slack_app_live.py --live
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from slack_sdk.web.async_client import AsyncWebClient


async def _wait_for_channel_message(
    client: AsyncWebClient,
    channel: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 45,
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    """Poll channel history until a message satisfying predicate appears."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    after = float(after_ts)
    last_error = None

    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.conversations_history(channel=channel, limit=50)
            for message in response.get("messages", []):
                try:
                    if float(message.get("ts", "0")) <= after:
                        continue
                except ValueError:
                    continue
                if predicate(message):
                    return message
        except Exception as exc:  # pragma: no cover - live network behavior
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for bot response after {timeout_seconds}s. Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for bot response after {timeout_seconds}s")


async def _wait_for_thread_message(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 90,
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    """Poll a thread until a message satisfying predicate appears."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    after = float(after_ts)
    last_error = None

    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=100,
            )
            for message in response.get("messages", []):
                try:
                    if float(message.get("ts", "0")) <= after:
                        continue
                except ValueError:
                    continue
                if predicate(message):
                    return message
        except Exception as exc:  # pragma: no cover - live network behavior
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for thread bot response after {timeout_seconds}s. Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for thread bot response after {timeout_seconds}s")


@pytest.mark.live
@pytest.mark.asyncio
async def test_app_mention_roundtrip(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Post a real app mention and assert the bot's event handler response."""
    marker = uuid.uuid4().hex[:8]
    mention_text = f"<@{slack_bot_user_id}> [Live App Test] mention-{marker}"
    user_message_ts = None
    bot_response_ts = None

    try:
        post = await slack_user_client.chat_postMessage(
            channel=slack_test_channel,
            text=mention_text,
        )
        assert post["ok"] is True
        user_message_ts = post["ts"]

        bot_message = await _wait_for_channel_message(
            client=slack_client,
            channel=slack_test_channel,
            after_ts=user_message_ts,
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and "Hi! I'm Claude Code Bot." in msg.get("text", "")
            ),
        )
        bot_response_ts = bot_message["ts"]
        assert "Hi! I'm Claude Code Bot." in bot_message.get("text", "")
    finally:
        if bot_response_ts:
            await slack_client.chat_delete(channel=slack_test_channel, ts=bot_response_ts)
        if user_message_ts:
            await slack_user_client.chat_delete(channel=slack_test_channel, ts=user_message_ts)


@pytest.mark.live
@pytest.mark.asyncio
async def test_thread_message_roundtrip(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Post a real thread reply and assert the bot responds in the same thread."""
    marker = uuid.uuid4().hex[:8]
    parent_ts = None
    thread_user_ts = None
    bot_response_ts = None

    try:
        parent = await slack_user_client.chat_postMessage(
            channel=slack_test_channel,
            text=f"[Live App Test] thread-parent-{marker}",
        )
        assert parent["ok"] is True
        parent_ts = parent["ts"]

        thread_reply = await slack_user_client.chat_postMessage(
            channel=slack_test_channel,
            thread_ts=parent_ts,
            text=f"[Live App Test] thread-reply-{marker}",
        )
        assert thread_reply["ok"] is True
        thread_user_ts = thread_reply["ts"]

        bot_message = await _wait_for_thread_message(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=parent_ts,
            after_ts=thread_user_ts,
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and msg.get("thread_ts") == parent_ts
            ),
        )
        bot_response_ts = bot_message["ts"]
    finally:
        if bot_response_ts:
            await slack_client.chat_delete(channel=slack_test_channel, ts=bot_response_ts)
        if thread_user_ts:
            await slack_user_client.chat_delete(channel=slack_test_channel, ts=thread_user_ts)
        if parent_ts:
            await slack_user_client.chat_delete(channel=slack_test_channel, ts=parent_ts)
