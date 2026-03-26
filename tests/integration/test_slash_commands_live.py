"""Live end-to-end tests for slash commands dispatched against real Slack.

Each test invokes a registered handler through ``SlashCommandDispatcher``
(see ``conftest.py``), which calls the handler with a real Slack bot client so
the response is posted to the live test channel.  We then poll for the bot's
reply and assert on its content.

Required environment variables:
- SLACK_BOT_TOKEN
- SLACK_USER_TOKEN
- SLACK_TEST_CHANNEL

Run with: pytest tests/integration/test_slash_commands_live.py --live -v
"""

import asyncio
import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.integration.conftest import SlashCommandDispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_channel_message(
    client: AsyncWebClient,
    channel: str,
    after_ts: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: int = 30,
    poll_seconds: float = 1.5,
) -> dict[str, Any]:
    """Poll channel history until a message satisfying *predicate* appears."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    after = float(after_ts)
    last_error = None

    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.conversations_history(channel=channel, limit=30)
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
        raise AssertionError(f"Timed out after {timeout_seconds}s. Last error: {last_error}")
    raise AssertionError(f"Timed out waiting for bot response after {timeout_seconds}s")


def _text_contains(fragment: str) -> Callable[[dict[str, Any]], bool]:
    def _pred(msg: dict[str, Any]) -> bool:
        return fragment in (msg.get("text") or "")

    return _pred


def _text_contains_any(*fragments: str) -> Callable[[dict[str, Any]], bool]:
    def _pred(msg: dict[str, Any]) -> bool:
        body = msg.get("text") or ""
        return any(f in body for f in fragments)

    return _pred


async def _dispatch_and_expect(
    dispatch: SlashCommandDispatcher,
    client: AsyncWebClient,
    channel: str,
    command: str,
    text: str,
    predicate: Callable[[dict[str, Any]], bool],
    thread_ts: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Invoke *command* via dispatcher, then poll for the matching bot reply."""
    # Record a timestamp anchor before dispatch
    anchor_resp = await client.conversations_history(channel=channel, limit=1)
    anchor_ts = anchor_resp["messages"][0]["ts"] if anchor_resp.get("messages") else "0"

    await dispatch.dispatch(command, text=text, thread_ts=thread_ts)

    return await _wait_for_channel_message(
        client=client,
        channel=channel,
        after_ts=anchor_ts,
        predicate=predicate,
        timeout_seconds=timeout_seconds,
    )


async def _delete_message(client: AsyncWebClient, channel: str, ts: str) -> None:
    try:
        await client.chat_delete(channel=channel, ts=ts)
    except Exception:
        pass


# ============================================================================
# /pwd
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_pwd(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/pwd returns the current working directory."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/pwd",
        "",
        _text_contains("Current working directory"),
    )
    try:
        assert "Current working directory" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /ls
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_ls_default(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/ls with no argument lists contents of the working directory."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "",
        _text_contains("Contents of"),
    )
    try:
        assert "Contents of" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_ls_with_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/ls /tmp lists contents of /tmp."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "/tmp",
        _text_contains("Contents of"),
    )
    try:
        assert "/tmp" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_ls_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/ls with a nonexistent path produces an error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "/nonexistent_path_abc123",
        _text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /cd
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_cd_no_args_shows_cwd(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cd with no argument shows the current working directory."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "",
        _text_contains("Current working directory"),
    )
    try:
        assert "Current working directory" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_cd_to_tmp(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cd /tmp changes the working directory."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "/tmp",
        _text_contains_any("Working directory updated", "/tmp"),
    )
    try:
        assert "/tmp" in msg["text"] or "updated" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_cd_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cd to a nonexistent path produces an error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "/nonexistent_path_abc123",
        _text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /! (bash execution)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_bang_echo(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/! echo hello executes and returns output."""
    marker = uuid.uuid4().hex[:8]
    # The handler posts a processing message then updates it
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/!",
        f"echo live-test-{marker}",
        _text_contains_any(f"live-test-{marker}", "Running"),
        timeout_seconds=30,
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_bang_failing_command(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/! false returns a non-zero exit code."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/!",
        "false",
        _text_contains_any("Running", "failed", "status 1", "no output"),
        timeout_seconds=30,
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /clear
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_clear(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/clear resets the conversation session."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/clear",
        "",
        _text_contains_any("Conversation cleared", "cleared"),
    )
    try:
        assert "clear" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /esc
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_esc_no_active(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/esc with no active operations reports nothing to interrupt."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/esc",
        "",
        _text_contains("No active operations"),
    )
    try:
        assert "No active operations" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /cancel (/c)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_cancel_no_active(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cancel with no running executions reports nothing to cancel."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cancel",
        "",
        _text_contains("No active executions"),
    )
    try:
        assert "No active executions" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_c_alias(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/c is an alias for /cancel."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/c",
        "",
        _text_contains("No active executions"),
    )
    try:
        assert "No active executions" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /add-dir, /remove-dir, /list-dirs
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_add_dir(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/add-dir /tmp adds the directory to context."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/add-dir",
        "/tmp",
        _text_contains("Directory Added"),
    )
    try:
        assert "Directory Added" in msg["text"] or "/tmp" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_add_dir_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/add-dir with nonexistent path produces an error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/add-dir",
        "/nonexistent_path_xyz789",
        _text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_list_dirs(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/list-dirs shows directories in context."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/list-dirs",
        "",
        _text_contains("Directories in context"),
    )
    try:
        assert "Directories in context" in msg["text"] or "Working directory" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_remove_dir_not_in_list(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/remove-dir on a directory not in context shows an appropriate message."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/remove-dir",
        "/not_in_context_dir",
        _text_contains("not in the context"),
    )
    try:
        assert "not in the context" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_add_then_remove_dir(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/add-dir then /remove-dir round-trips correctly."""
    messages_to_delete: list[str] = []
    try:
        msg1 = await _dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/add-dir",
            "/tmp",
            _text_contains("Directory Added"),
        )
        messages_to_delete.append(msg1["ts"])

        # /tmp resolves to its real path
        resolved_tmp = str(os.path.realpath("/tmp"))
        msg2 = await _dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/remove-dir",
            resolved_tmp,
            _text_contains("Directory Removed"),
        )
        messages_to_delete.append(msg2["ts"])
        assert "Directory Removed" in msg2["text"]
    finally:
        for ts in messages_to_delete:
            await _delete_message(slack_client, slack_test_channel, ts)


# ============================================================================
# /hist (/h)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_hist_empty_session(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/hist in a fresh session shows no history."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/hist",
        "",
        _text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_h_alias(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/h is an alias for /hist."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/h",
        "",
        _text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_hist_invalid_index(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/hist abc produces a parse error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/hist",
        "abc",
        _text_contains("positive integers"),
    )
    try:
        assert "positive integers" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /diff
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_diff_empty_session(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/diff in a fresh session shows no history."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/diff",
        "",
        _text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /mode
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_show(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode with no args shows current permission mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "",
        _text_contains_any("Current mode", "Current permission mode"),
    )
    try:
        body = msg["text"]
        assert "mode" in body.lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_bypass(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode bypass changes to bypass mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "bypass",
        _text_contains_any("Mode changed", "bypass"),
    )
    try:
        body = msg["text"]
        assert "bypass" in body.lower() or "Mode changed" in body
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_plan(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode plan changes to plan mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "plan",
        _text_contains_any("Mode changed", "plan"),
    )
    try:
        body = msg["text"]
        assert "plan" in body.lower() or "Mode changed" in body
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_accept(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode accept changes to acceptEdits mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "accept",
        _text_contains_any("Mode changed", "accept"),
    )
    try:
        body = msg["text"]
        assert "accept" in body.lower() or "Mode changed" in body
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_ask(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode ask changes to default mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "ask",
        _text_contains_any("Mode changed", "ask", "default"),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_delegate(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode delegate changes to delegate mode."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "delegate",
        _text_contains_any("Mode changed", "delegate"),
    )
    try:
        body = msg["text"]
        assert "delegate" in body.lower() or "Mode changed" in body
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_invalid(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode notamode produces an error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "notamode",
        _text_contains_any("Unknown mode", "notamode"),
    )
    try:
        assert "Unknown mode" in msg["text"] or "notamode" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /notifications
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_show(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications shows current settings."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "",
        _text_contains_any("Notification settings", "Notification Settings"),
    )
    try:
        assert "notification" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications on enables all notifications."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "on",
        _text_contains("Notifications enabled"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications off disables all notifications."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "off",
        _text_contains("Notifications disabled"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_completion_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications completion on enables completion alerts."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "completion on",
        _text_contains("Completion notifications"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_completion_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications completion off disables completion alerts."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "completion off",
        _text_contains("Completion notifications"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_permission_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications permission on enables permission alerts."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "permission on",
        _text_contains("Permission notifications"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_permission_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications permission off disables permission alerts."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "permission off",
        _text_contains("Permission notifications"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_invalid_subcommand(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications garbage shows an error."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "garbage",
        _text_contains("Unknown subcommand"),
    )
    try:
        assert "Unknown subcommand" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /model
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_model_show_picker(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/model with no args shows the model selection picker."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "",
        _text_contains_any("Current model", "Select model"),
    )
    try:
        assert "model" in msg["text"].lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_model_set_by_name(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/model sonnet sets the model."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "sonnet",
        _text_contains("Model changed"),
    )
    try:
        assert "Model changed" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_model_set_with_effort(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/model sonnet high sets model with effort."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "sonnet high",
        _text_contains("Model changed"),
    )
    try:
        assert "Model changed" in msg["text"]
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /qv (queue view)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_qv_empty_queue(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qv on an empty queue shows no items or queue status."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qv",
        "",
        _text_contains_any("queue", "Queue", "No", "empty"),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /qclear
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_qclear(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qclear on an empty queue clears nothing gracefully."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qclear",
        "",
        _text_contains_any("clear", "Clear", "queue", "Queue", "0", "No"),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /qdelete
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_qdelete_empty_queue(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qdelete on an empty queue."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qdelete",
        "",
        _text_contains_any("delet", "Delet", "queue", "Queue", "0", "No"),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /permissions
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_permissions(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/permissions shows current permission settings."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/permissions",
        "",
        _text_contains_any("permission", "Permission", "mode", "Mode"),
    )
    try:
        body = msg["text"]
        assert "permission" in body.lower() or "mode" in body.lower()
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /st (status)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_st_no_jobs(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/st with no active jobs."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/st",
        "",
        _text_contains_any("No active", "no active", "status", "Status", "job", "Job"),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /usage (session info for Claude backend)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_usage(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/usage shows session status summary."""
    msg = await _dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/usage",
        "",
        _text_contains_any(
            "Session",
            "session",
            "model",
            "Model",
            "Working dir",
            "working dir",
            "Usage",
        ),
    )
    try:
        assert msg.get("ts")
    finally:
        await _delete_message(slack_client, slack_test_channel, msg["ts"])
