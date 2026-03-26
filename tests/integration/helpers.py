"""Shared helpers for live integration tests.

Provides polling utilities, predicate factories, and cleanup management so that
individual test modules stay focused on test logic.

Pass ``--keep-messages`` to pytest to skip cleanup and leave all Slack messages
in the channel for manual inspection.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

# Set to True by conftest when --keep-messages is passed.
KEEP_MESSAGES: bool = False

# Default retry pause for Slack rate limits (429).
_RATE_LIMIT_PAUSE: float = 3.0


# ---------------------------------------------------------------------------
# Rate-limit-aware Slack API call
# ---------------------------------------------------------------------------


async def slack_post_with_retry(
    client: AsyncWebClient,
    *,
    max_retries: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call ``chat_postMessage`` with automatic retry on 429 rate-limit errors."""
    for attempt in range(max_retries):
        try:
            return await client.chat_postMessage(**kwargs)
        except SlackApiError as exc:
            if exc.response.get("error") == "ratelimited" and attempt < max_retries - 1:
                retry_after = float(exc.response.headers.get("Retry-After", _RATE_LIMIT_PAUSE))
                await asyncio.sleep(retry_after)
                continue
            raise


# ---------------------------------------------------------------------------
# Message posting
# ---------------------------------------------------------------------------


async def post_user_message_or_skip(
    client: AsyncWebClient,
    *,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Post a user-scoped message or skip the test if scopes are missing."""
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    try:
        return await client.chat_postMessage(**kwargs)
    except SlackApiError as exc:
        if exc.response.get("error") != "missing_scope":
            raise
        needed = exc.response.get("needed", "unknown")
        provided = exc.response.get("provided", "unknown")
        pytest.skip(
            f"SLACK_USER_TOKEN missing required Slack scope: needed={needed}, provided={provided}"
        )


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


async def wait_for_bot_reply(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 60,
    poll_seconds: float = 1.5,
) -> dict[str, Any]:
    """Poll a thread until a bot message satisfying *predicate* appears."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    after = float(after_ts)
    last_error = None

    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.conversations_replies(channel=channel, ts=thread_ts, limit=100)
            for message in response.get("messages", []):
                try:
                    if float(message.get("ts", "0")) <= after:
                        continue
                except ValueError:
                    continue
                if predicate(message):
                    return message
        except SlackApiError as exc:
            if exc.response.get("error") == "ratelimited":
                await asyncio.sleep(
                    float(exc.response.headers.get("Retry-After", _RATE_LIMIT_PAUSE))
                )
                continue
            last_error = exc
        except Exception as exc:  # pragma: no cover - live network behavior
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for bot response after {timeout_seconds}s. "
            f"Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for bot response after {timeout_seconds}s")


async def wait_for_channel_message(
    client: AsyncWebClient,
    channel: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 60,
    poll_seconds: float = 1.5,
) -> dict[str, Any]:
    """Poll channel history until a message satisfying *predicate* appears."""
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
        except SlackApiError as exc:
            if exc.response.get("error") == "ratelimited":
                await asyncio.sleep(
                    float(exc.response.headers.get("Retry-After", _RATE_LIMIT_PAUSE))
                )
                continue
            last_error = exc
        except Exception as exc:  # pragma: no cover
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for channel message after {timeout_seconds}s. "
            f"Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for channel message after {timeout_seconds}s")


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def text_contains(fragment: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message text contains *fragment*."""

    def _predicate(msg: dict[str, Any]) -> bool:
        return fragment in (msg.get("text") or "")

    return _predicate


def text_contains_any(*fragments: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message text contains any of *fragments*."""

    def _predicate(msg: dict[str, Any]) -> bool:
        body = msg.get("text") or ""
        return any(f in body for f in fragments)

    return _predicate


def is_bot_message(bot_user_id: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message is from the given bot user."""

    def _predicate(msg: dict[str, Any]) -> bool:
        return msg.get("user") == bot_user_id

    return _predicate


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class MessageCleanup:
    """Collect message timestamps for deferred deletion at the end of a test."""

    def __init__(
        self,
        bot_client: AsyncWebClient,
        user_client: AsyncWebClient,
        channel: str,
    ) -> None:
        self._bot = bot_client
        self._user = user_client
        self._channel = channel
        self._bot_timestamps: list[str] = []
        self._user_timestamps: list[str] = []

    def track_bot(self, ts: str) -> None:
        self._bot_timestamps.append(ts)

    def track_user(self, ts: str) -> None:
        self._user_timestamps.append(ts)

    async def cleanup(self) -> None:
        if KEEP_MESSAGES:
            return
        for ts in self._bot_timestamps:
            try:
                await self._bot.chat_delete(channel=self._channel, ts=ts)
            except Exception:
                pass
        for ts in self._user_timestamps:
            try:
                await self._user.chat_delete(channel=self._channel, ts=ts)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Combined send-and-expect
# ---------------------------------------------------------------------------


async def send_and_expect(
    bot_client: AsyncWebClient,
    user_client: AsyncWebClient,
    channel: str,
    text: str,
    predicate: Callable[[dict[str, Any]], bool],
    thread_ts: str | None = None,
    timeout_seconds: int = 60,
) -> tuple[dict[str, Any], MessageCleanup]:
    """Post *text* as user, wait for a bot reply matching *predicate*, return both."""
    cleanup = MessageCleanup(bot_client, user_client, channel)

    post = await post_user_message_or_skip(
        user_client, channel=channel, text=text, thread_ts=thread_ts
    )
    assert post["ok"] is True
    user_ts = post["ts"]
    cleanup.track_user(user_ts)

    effective_thread = thread_ts or user_ts
    bot_msg = await wait_for_bot_reply(
        client=bot_client,
        channel=channel,
        thread_ts=effective_thread,
        after_ts=user_ts,
        predicate=predicate,
        timeout_seconds=timeout_seconds,
    )
    cleanup.track_bot(bot_msg["ts"])
    return bot_msg, cleanup


# ---------------------------------------------------------------------------
# Slash command dispatch helper
# ---------------------------------------------------------------------------


async def dispatch_and_expect(
    dispatch,
    client: AsyncWebClient,
    channel: str,
    command: str,
    text: str,
    predicate: Callable[[dict[str, Any]], bool],
    thread_ts: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Invoke *command* via dispatcher, then poll for the matching bot reply."""
    for _attempt in range(3):
        try:
            anchor_resp = await client.conversations_history(channel=channel, limit=1)
            break
        except SlackApiError as exc:
            if exc.response.get("error") == "ratelimited":
                await asyncio.sleep(
                    float(exc.response.headers.get("Retry-After", _RATE_LIMIT_PAUSE))
                )
                continue
            raise
    else:
        anchor_resp = {"messages": []}
    anchor_ts = anchor_resp["messages"][0]["ts"] if anchor_resp.get("messages") else "0"

    await dispatch.dispatch(command, text=text, thread_ts=thread_ts)

    return await wait_for_channel_message(
        client=client,
        channel=channel,
        after_ts=anchor_ts,
        predicate=predicate,
        timeout_seconds=timeout_seconds,
    )


async def delete_message(client: AsyncWebClient, channel: str, ts: str) -> None:
    """Delete a message, swallowing errors. No-op when ``--keep-messages`` is active."""
    if KEEP_MESSAGES:
        return
    try:
        await client.chat_delete(channel=channel, ts=ts)
    except Exception:
        pass
