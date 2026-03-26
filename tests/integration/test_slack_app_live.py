"""Live end-to-end tests for Slack app event handlers.

These tests validate that the running Slack app receives real events and responds.

Required environment variables:
- SLACK_BOT_TOKEN
- SLACK_USER_TOKEN
- SLACK_TEST_CHANNEL

Run with: pytest tests/integration/test_slack_app_live.py --live
"""

import uuid

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.integration.helpers import (
    post_user_message_or_skip,
    wait_for_bot_reply,
    wait_for_channel_message,
)


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
        post = await post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=mention_text,
        )
        assert post["ok"] is True
        user_message_ts = post["ts"]

        bot_message = await wait_for_channel_message(
            client=slack_client,
            channel=slack_test_channel,
            after_ts=user_message_ts,
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and "Hi! I'm the code assistant bot." in msg.get("text", "")
            ),
        )
        bot_response_ts = bot_message["ts"]
        assert "Hi! I'm the code assistant bot." in bot_message.get("text", "")
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
        parent = await post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[Live App Test] thread-parent-{marker}",
        )
        assert parent["ok"] is True
        parent_ts = parent["ts"]

        thread_reply = await post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            thread_ts=parent_ts,
            text=f"[Live App Test] thread-reply-{marker}",
        )
        assert thread_reply["ok"] is True
        thread_user_ts = thread_reply["ts"]

        bot_message = await wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=parent_ts,
            after_ts=thread_user_ts,
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id and msg.get("thread_ts") == parent_ts
            ),
            timeout_seconds=90,
        )
        bot_response_ts = bot_message["ts"]
    finally:
        if bot_response_ts:
            await slack_client.chat_delete(channel=slack_test_channel, ts=bot_response_ts)
        if thread_user_ts:
            await slack_user_client.chat_delete(channel=slack_test_channel, ts=thread_user_ts)
        if parent_ts:
            await slack_user_client.chat_delete(channel=slack_test_channel, ts=parent_ts)
