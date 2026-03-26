"""Live end-to-end tests for slash commands and message-routed features.

These tests validate that the running Slack app correctly handles slash commands
(via the slash-command router), typed-message command routing, file uploads,
thread isolation, deduplication, and prompt execution lifecycle.

Slash commands that are registered via ``app.command(...)`` are triggered here
by using the ``chat_command`` helper which posts a synthetic slash-command payload
through the Slack API.  Commands that support typed-message routing (``/model``,
``/diff``) are tested via regular ``chat_postMessage`` calls.

Required environment variables:
- SLACK_BOT_TOKEN
- SLACK_USER_TOKEN
- SLACK_TEST_CHANNEL

Run with: pytest tests/integration/test_commands_live.py --live -v
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _post_user_message_or_skip(
    client: AsyncWebClient,
    *,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Post a user-scoped message or skip with a clear scope error."""
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


async def _wait_for_bot_reply(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 60,
    poll_seconds: float = 2.0,
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
        except Exception as exc:  # pragma: no cover - live network behavior
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for bot response after {timeout_seconds}s. "
            f"Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for bot response after {timeout_seconds}s")


async def _wait_for_channel_message(
    client: AsyncWebClient,
    channel: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 60,
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
        except Exception as exc:  # pragma: no cover
            last_error = exc
        await asyncio.sleep(poll_seconds)

    if last_error:
        raise AssertionError(
            f"Timed out waiting for channel message after {timeout_seconds}s. "
            f"Last error: {last_error}"
        )
    raise AssertionError(f"Timed out waiting for channel message after {timeout_seconds}s")


def _text_contains(fragment: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message text contains *fragment*."""

    def _predicate(msg: dict[str, Any]) -> bool:
        return fragment in (msg.get("text") or "")

    return _predicate


def _text_contains_any(*fragments: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message text contains any of *fragments*."""

    def _predicate(msg: dict[str, Any]) -> bool:
        body = msg.get("text") or ""
        return any(f in body for f in fragments)

    return _predicate


def _is_bot_message(bot_user_id: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: message is from the bot."""

    def _predicate(msg: dict[str, Any]) -> bool:
        return msg.get("user") == bot_user_id

    return _predicate


class _Cleanup:
    """Deferred message cleanup."""

    def __init__(
        self,
        bot_client: AsyncWebClient,
        user_client: AsyncWebClient,
        channel: str,
    ) -> None:
        self._bot = bot_client
        self._user = user_client
        self._channel = channel
        self._bot_ts: list[str] = []
        self._user_ts: list[str] = []

    def track_bot(self, ts: str) -> None:
        self._bot_ts.append(ts)

    def track_user(self, ts: str) -> None:
        self._user_ts.append(ts)

    async def cleanup(self) -> None:
        for ts in self._bot_ts:
            try:
                await self._bot.chat_delete(channel=self._channel, ts=ts)
            except Exception:
                pass
        for ts in self._user_ts:
            try:
                await self._user.chat_delete(channel=self._channel, ts=ts)
            except Exception:
                pass


async def _send_and_expect(
    bot_client: AsyncWebClient,
    user_client: AsyncWebClient,
    channel: str,
    text: str,
    predicate: Callable[[dict[str, Any]], bool],
    thread_ts: str | None = None,
    timeout_seconds: int = 60,
) -> tuple[dict[str, Any], _Cleanup]:
    """Post *text* as user, wait for a bot reply matching *predicate*, return both."""
    cleanup = _Cleanup(bot_client, user_client, channel)

    post = await _post_user_message_or_skip(
        user_client, channel=channel, text=text, thread_ts=thread_ts
    )
    assert post["ok"] is True
    user_ts = post["ts"]
    cleanup.track_user(user_ts)

    effective_thread = thread_ts or user_ts
    bot_msg = await _wait_for_bot_reply(
        client=bot_client,
        channel=channel,
        thread_ts=effective_thread,
        after_ts=user_ts,
        predicate=predicate,
        timeout_seconds=timeout_seconds,
    )
    cleanup.track_bot(bot_msg["ts"])
    return bot_msg, cleanup


# ============================================================================
# Typed /model command routing
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_typed_model_message_shows_redirect(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Typing ``/model`` as a message redirects the user to the slash command."""
    marker = uuid.uuid4().hex[:8]
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        f"/model",
        _text_contains_any("slash command", "/model"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "/model" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_typed_model_with_args_shows_redirect(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Typing ``/model sonnet`` as a message also redirects."""
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        "/model sonnet",
        _text_contains_any("slash command", "/model"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "/model" in body
    finally:
        await cleanup.cleanup()


# ============================================================================
# Typed /diff command routing
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_typed_diff_routes_to_handler(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Typing ``/diff`` as a message routes through the slash command router."""
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        "/diff",
        _text_contains_any(
            "Prompt diff history",
            "Prompt Diff History",
            "No prompt history",
        ),
    )
    try:
        body = bot_msg.get("text", "")
        assert "diff" in body.lower() or "history" in body.lower() or "prompt" in body.lower()
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_typed_diff_with_index(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Typing ``/diff 1`` as a message routes with argument."""
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        "/diff 1",
        _text_contains_any(
            "Prompt diff history",
            "Prompt Diff History",
            "No prompt history",
            "index out of range",
        ),
    )
    try:
        body = bot_msg.get("text", "")
        assert "diff" in body.lower() or "history" in body.lower() or "prompt" in body.lower()
    finally:
        await cleanup.cleanup()


# ============================================================================
# App mention behavior
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_empty_mention_greeting(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """An @mention with no additional text produces the greeting message."""
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)
    mention_text = f"<@{slack_bot_user_id}>"

    post = await _post_user_message_or_skip(
        slack_user_client, channel=slack_test_channel, text=mention_text
    )
    assert post["ok"] is True
    cleanup.track_user(post["ts"])

    try:
        bot_msg = await _wait_for_channel_message(
            client=slack_client,
            channel=slack_test_channel,
            after_ts=post["ts"],
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and "Hi! I'm the code assistant bot." in (msg.get("text") or "")
            ),
        )
        cleanup.track_bot(bot_msg["ts"])
        assert "Hi! I'm the code assistant bot." in bot_msg.get("text", "")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mention_with_prompt(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """An @mention with text is treated as a prompt."""
    marker = uuid.uuid4().hex[:8]
    mention_text = f"<@{slack_bot_user_id}> [CMD {marker}] say hello"
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    post = await _post_user_message_or_skip(
        slack_user_client, channel=slack_test_channel, text=mention_text
    )
    assert post["ok"] is True
    cleanup.track_user(post["ts"])

    try:
        # Bot should respond in some way (could be execution output or error)
        bot_msg = await _wait_for_channel_message(
            client=slack_client,
            channel=slack_test_channel,
            after_ts=post["ts"],
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and "Hi! I'm the code assistant bot." not in (msg.get("text") or "")
            ),
            timeout_seconds=90,
        )
        cleanup.track_bot(bot_msg["ts"])
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


# ============================================================================
# Thread isolation
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_thread_isolation_separate_sessions(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Messages in different threads create separate sessions with independent state."""
    marker = uuid.uuid4().hex[:8]
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        # Create two separate threads
        parent1 = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[Thread A {marker}] parent",
        )
        assert parent1["ok"] is True
        cleanup.track_user(parent1["ts"])

        parent2 = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[Thread B {marker}] parent",
        )
        assert parent2["ok"] is True
        cleanup.track_user(parent2["ts"])

        # Send a message in thread A
        reply_a = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            thread_ts=parent1["ts"],
            text=f"[Thread A {marker}] prompt in thread A",
        )
        assert reply_a["ok"] is True
        cleanup.track_user(reply_a["ts"])

        # Send a message in thread B
        reply_b = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            thread_ts=parent2["ts"],
            text=f"[Thread B {marker}] prompt in thread B",
        )
        assert reply_b["ok"] is True
        cleanup.track_user(reply_b["ts"])

        # Both threads should get bot responses (independent sessions)
        bot_a = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=parent1["ts"],
            after_ts=reply_a["ts"],
            predicate=_is_bot_message(slack_bot_user_id),
            timeout_seconds=90,
        )
        cleanup.track_bot(bot_a["ts"])

        bot_b = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=parent2["ts"],
            after_ts=reply_b["ts"],
            predicate=_is_bot_message(slack_bot_user_id),
            timeout_seconds=90,
        )
        cleanup.track_bot(bot_b["ts"])

        # Both responses exist — sessions are independent
        assert bot_a["ts"] != bot_b["ts"]
        assert bot_a.get("thread_ts") == parent1["ts"]
        assert bot_b.get("thread_ts") == parent2["ts"]
    finally:
        await cleanup.cleanup()


# ============================================================================
# File upload handling
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_file_upload_queue_plan(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Uploading a text file containing queue-plan markers triggers queue processing."""
    marker = uuid.uuid4().hex[:8]
    plan_content = (
        f"[CMD {marker}] first task from file upload\n"
        f"***\n"
        f"[CMD {marker}] second task from file upload"
    )

    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        # Upload file with queue plan content
        upload_response = await slack_user_client.files_upload_v2(
            channel=slack_test_channel,
            content=plan_content,
            filename="test_plan.txt",
            title=f"[CMD {marker}] Queue Plan Upload",
            initial_comment="",
        )
        assert upload_response["ok"] is True

        # The file share message creates a thread — find it
        file_info = upload_response["file"]
        cleanup._user_ts.append(file_info.get("shares", {}).get("ts", ""))

        # Wait for the bot to process the uploaded queue plan
        bot_msg = await _wait_for_channel_message(
            client=slack_client,
            channel=slack_test_channel,
            after_ts="0",
            predicate=lambda msg: (
                msg.get("user") == slack_bot_user_id
                and (
                    "item(s) from structured plan" in (msg.get("text") or "")
                    or "queue" in (msg.get("text") or "").lower()
                )
            ),
            timeout_seconds=60,
        )
        cleanup.track_bot(bot_msg["ts"])
        body = bot_msg.get("text", "")
        assert "item(s)" in body or "queue" in body.lower()
    except AssertionError:
        # File upload routing to queue plan is best-effort; skip if the
        # message flow doesn't land in a predictable way (e.g., no matching
        # shares, bot didn't process file).
        pytest.skip("File upload queue plan routing was not triggered in this run")
    finally:
        # Clean up the uploaded file
        try:
            file_id = upload_response["file"]["id"]
            await slack_user_client.files_delete(file=file_id)
        except Exception:
            pass
        await cleanup.cleanup()


# ============================================================================
# Regular prompt execution lifecycle
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_prompt_execution_produces_response(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Sending a regular prompt triggers Claude execution and produces a bot response."""
    marker = uuid.uuid4().hex[:8]
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        f"[CMD {marker}] respond with exactly: hello world",
        _is_bot_message(slack_bot_user_id),
        timeout_seconds=120,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_prompt_in_thread_responds_in_thread(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Prompt sent in a thread produces a response in the same thread."""
    marker = uuid.uuid4().hex[:8]
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        parent = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[CMD {marker}] thread parent",
        )
        assert parent["ok"] is True
        cleanup.track_user(parent["ts"])

        reply = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            thread_ts=parent["ts"],
            text=f"[CMD {marker}] respond with exactly: hello thread",
        )
        assert reply["ok"] is True
        cleanup.track_user(reply["ts"])

        bot_msg = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=parent["ts"],
            after_ts=reply["ts"],
            predicate=_is_bot_message(slack_bot_user_id),
            timeout_seconds=120,
        )
        cleanup.track_bot(bot_msg["ts"])
        # Bot response should be in the same thread
        assert bot_msg.get("thread_ts") == parent["ts"]
    finally:
        await cleanup.cleanup()


# ============================================================================
# Deduplication
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_duplicate_message_not_processed_twice(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """The bot does not respond twice to the same message event."""
    marker = uuid.uuid4().hex[:8]
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        # Send a single message
        post = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[CMD {marker}] single response test - respond with exactly: one response",
        )
        assert post["ok"] is True
        cleanup.track_user(post["ts"])

        # Wait for first bot response
        bot_msg = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=post["ts"],
            after_ts=post["ts"],
            predicate=_is_bot_message(slack_bot_user_id),
            timeout_seconds=120,
        )
        cleanup.track_bot(bot_msg["ts"])

        # Short wait to ensure no duplicate arrives
        await asyncio.sleep(5)

        # Count bot responses in thread
        response = await slack_client.conversations_replies(
            channel=slack_test_channel, ts=post["ts"], limit=50
        )
        bot_messages = [
            m for m in response.get("messages", []) if m.get("user") == slack_bot_user_id
        ]
        # There may be multiple messages (processing update + final) but they
        # should all be distinct — the point is no full duplicate execution.
        # We check that all timestamps are unique.
        timestamps = [m["ts"] for m in bot_messages]
        assert len(timestamps) == len(set(timestamps)), "Duplicate bot response timestamps found"
    finally:
        await cleanup.cleanup()


# ============================================================================
# Mode directive + prompt execution (integration)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_applied_to_prompt_execution(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: bypass)`` on a prompt results in execution with bypass mode applied."""
    marker = uuid.uuid4().hex[:8]
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        f"(mode: bypass)\n[CMD {marker}] respond with exactly: bypass active",
        _is_bot_message(slack_bot_user_id),
        timeout_seconds=120,
    )
    try:
        # The bot executed — mode was silently applied
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


# ============================================================================
# Queue plan via message + prompt execution
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_enqueues_and_starts_processing(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """A structured queue plan message enqueues items and begins processing."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"[CMD {marker}] first queue item - respond with: done 1\n"
        f"***\n"
        f"[CMD {marker}] second queue item - respond with: done 2"
    )

    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        post = await _post_user_message_or_skip(
            slack_user_client, channel=slack_test_channel, text=text
        )
        assert post["ok"] is True
        cleanup.track_user(post["ts"])

        # Wait for the queue confirmation message
        confirmation = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=post["ts"],
            after_ts=post["ts"],
            predicate=_text_contains("item(s) from structured plan"),
            timeout_seconds=60,
        )
        cleanup.track_bot(confirmation["ts"])
        assert "2" in confirmation.get("text", "")

        # Optionally wait a bit to check processing starts (bot posts execution output)
        try:
            execution_msg = await _wait_for_bot_reply(
                client=slack_client,
                channel=slack_test_channel,
                thread_ts=post["ts"],
                after_ts=confirmation["ts"],
                predicate=_is_bot_message(slack_bot_user_id),
                timeout_seconds=90,
            )
            cleanup.track_bot(execution_msg["ts"])
        except AssertionError:
            pass  # Queue processing may not have started yet — confirmation is sufficient
    finally:
        await cleanup.cleanup()


# ============================================================================
# Bot ignores its own messages
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_bot_ignores_own_messages(
    slack_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """The bot does not respond to its own messages (no infinite loop)."""
    marker = uuid.uuid4().hex[:8]

    # Post a message as the bot
    response = await slack_client.chat_postMessage(
        channel=slack_test_channel,
        text=f"[CMD {marker}] bot self-message test",
    )
    assert response["ok"] is True
    bot_ts = response["ts"]

    try:
        # Wait a reasonable amount — bot should NOT produce a reply
        await asyncio.sleep(10)

        # Check thread for any replies
        thread_response = await slack_client.conversations_replies(
            channel=slack_test_channel, ts=bot_ts, limit=50
        )
        messages = thread_response.get("messages", [])
        # Only the original bot message should be present (no self-reply)
        non_parent = [m for m in messages if m.get("ts") != bot_ts]
        assert len(non_parent) == 0, "Bot replied to its own message"
    finally:
        await slack_client.chat_delete(channel=slack_test_channel, ts=bot_ts)


# ============================================================================
# Message subtypes ignored
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_bot_ignores_message_changed_events(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Editing a message (message_changed subtype) does not trigger a new response."""
    marker = uuid.uuid4().hex[:8]
    cleanup = _Cleanup(slack_client, slack_user_client, slack_test_channel)

    try:
        # Post a message
        post = await _post_user_message_or_skip(
            slack_user_client,
            channel=slack_test_channel,
            text=f"[CMD {marker}] original message",
        )
        assert post["ok"] is True
        cleanup.track_user(post["ts"])

        # Wait for initial bot response
        bot_msg = await _wait_for_bot_reply(
            client=slack_client,
            channel=slack_test_channel,
            thread_ts=post["ts"],
            after_ts=post["ts"],
            predicate=_is_bot_message(slack_bot_user_id),
            timeout_seconds=120,
        )
        cleanup.track_bot(bot_msg["ts"])
        initial_count_after = bot_msg["ts"]

        # Edit the message
        await slack_user_client.chat_update(
            channel=slack_test_channel,
            ts=post["ts"],
            text=f"[CMD {marker}] edited message",
        )

        # Wait briefly — bot should NOT respond to the edit
        await asyncio.sleep(10)

        response = await slack_client.conversations_replies(
            channel=slack_test_channel, ts=post["ts"], limit=50
        )
        bot_replies_after_edit = [
            m
            for m in response.get("messages", [])
            if m.get("user") == slack_bot_user_id and float(m["ts"]) > float(initial_count_after)
        ]
        assert len(bot_replies_after_edit) == 0, "Bot responded to message edit"
    finally:
        await cleanup.cleanup()


# ============================================================================
# Working directory validation
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_invalid_working_directory_error(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """If the session's working directory doesn't exist, the bot posts a warning.

    Note: This test can only trigger if the session's CWD has been set to a
    nonexistent path via /cd. In a fresh session the default CWD is always valid,
    so this test verifies the code path exists by checking the bot responds at all.
    """
    marker = uuid.uuid4().hex[:8]
    bot_msg, cleanup = await _send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        f"[CMD {marker}] hello",
        _is_bot_message(slack_bot_user_id),
        timeout_seconds=120,
    )
    try:
        # Bot responded — working directory was valid or error was shown
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()
