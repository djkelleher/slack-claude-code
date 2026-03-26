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

import os
import uuid

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.integration.conftest import SlashCommandDispatcher
from tests.integration.helpers import (
    delete_message,
    dispatch_and_expect,
    text_contains,
    text_contains_any,
)

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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/pwd",
        "",
        text_contains("Current working directory"),
    )
    try:
        assert "Current working directory" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "",
        text_contains("Contents of"),
    )
    try:
        assert "Contents of" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_ls_with_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/ls /tmp lists contents of /tmp."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "/tmp",
        text_contains("Contents of"),
    )
    try:
        assert "/tmp" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_ls_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/ls with a nonexistent path produces an error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/ls",
        "/nonexistent_path_abc123",
        text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "",
        text_contains("Current working directory"),
    )
    try:
        assert "Current working directory" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_cd_to_tmp(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cd /tmp changes the working directory."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "/tmp",
        text_contains_any("Working directory updated", "/tmp"),
    )
    try:
        assert "/tmp" in msg["text"] or "updated" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_cd_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cd to a nonexistent path produces an error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cd",
        "/nonexistent_path_abc123",
        text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/!",
        f"echo live-test-{marker}",
        text_contains_any(f"live-test-{marker}", "Running"),
        timeout_seconds=30,
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_bang_failing_command(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/! false returns a non-zero exit code."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/!",
        "false",
        text_contains_any("Running", "failed", "status 1", "no output"),
        timeout_seconds=30,
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/clear",
        "",
        text_contains_any("Conversation cleared", "cleared"),
    )
    try:
        assert "clear" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/esc",
        "",
        text_contains("No active operations"),
    )
    try:
        assert "No active operations" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cancel",
        "",
        text_contains("No active executions"),
    )
    try:
        assert "No active executions" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_c_alias(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/c is an alias for /cancel."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/c",
        "",
        text_contains("No active executions"),
    )
    try:
        assert "No active executions" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/add-dir",
        "/tmp",
        text_contains("Added directory"),
    )
    try:
        assert "Added directory" in msg["text"] or "/tmp" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_add_dir_invalid_path(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/add-dir with nonexistent path produces an error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/add-dir",
        "/nonexistent_path_xyz789",
        text_contains("does not exist"),
    )
    try:
        assert "does not exist" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_list_dirs(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/list-dirs shows directories in context."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/list-dirs",
        "",
        text_contains("Directories in context"),
    )
    try:
        assert "Directories in context" in msg["text"] or "Working directory" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_remove_dir_not_in_list(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/remove-dir on a directory not in context shows an appropriate message."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/remove-dir",
        "/not_in_context_dir",
        text_contains("not found"),
    )
    try:
        assert "not found" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
        msg1 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/add-dir",
            "/tmp",
            text_contains("Added directory"),
        )
        messages_to_delete.append(msg1["ts"])

        # /tmp resolves to its real path
        resolved_tmp = str(os.path.realpath("/tmp"))
        msg2 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/remove-dir",
            resolved_tmp,
            text_contains("Removed directory"),
        )
        messages_to_delete.append(msg2["ts"])
        assert "Removed directory" in msg2["text"]
    finally:
        for ts in messages_to_delete:
            await delete_message(slack_client, slack_test_channel, ts)


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/hist",
        "",
        text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_h_alias(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/h is an alias for /hist."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/h",
        "",
        text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_hist_invalid_index(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/hist abc produces a parse error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/hist",
        "abc",
        text_contains("positive integers"),
    )
    try:
        assert "positive integers" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/diff",
        "",
        text_contains("No prompt history"),
    )
    try:
        assert "No prompt history" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "",
        text_contains_any("Current mode", "Current permission mode"),
    )
    try:
        body = msg["text"]
        assert "mode" in body.lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_bypass(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode bypass changes to bypass mode."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "bypass",
        text_contains_any("Mode changed", "bypass"),
    )
    try:
        body = msg["text"]
        assert "bypass" in body.lower() or "Mode changed" in body
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_plan(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode plan changes to plan mode."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "plan",
        text_contains_any("Mode changed", "plan"),
    )
    try:
        body = msg["text"]
        assert "plan" in body.lower() or "Mode changed" in body
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_accept(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode accept changes to acceptEdits mode."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "accept",
        text_contains_any("Mode changed", "accept"),
    )
    try:
        body = msg["text"]
        assert "accept" in body.lower() or "Mode changed" in body
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_ask(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode ask changes to default mode."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "ask",
        text_contains_any("Mode changed", "ask", "default"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_set_delegate(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode delegate changes to delegate mode."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "delegate",
        text_contains_any("Mode changed", "delegate"),
    )
    try:
        body = msg["text"]
        assert "delegate" in body.lower() or "Mode changed" in body
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_invalid(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode notamode produces an error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/mode",
        "notamode",
        text_contains_any("Unknown mode", "notamode"),
    )
    try:
        assert "Unknown mode" in msg["text"] or "notamode" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "",
        text_contains_any("Notification settings", "Notification Settings"),
    )
    try:
        assert "notification" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications on enables all notifications."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "on",
        text_contains("Notifications enabled"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications off disables all notifications."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "off",
        text_contains("Notifications disabled"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_completion_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications completion on enables completion alerts."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "completion on",
        text_contains("Completion notifications"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_completion_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications completion off disables completion alerts."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "completion off",
        text_contains("Completion notifications"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_permission_on(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications permission on enables permission alerts."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "permission on",
        text_contains("Permission notifications"),
    )
    try:
        assert "enabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_permission_off(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications permission off disables permission alerts."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "permission off",
        text_contains("Permission notifications"),
    )
    try:
        assert "disabled" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_invalid_subcommand(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications garbage shows an error."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/notifications",
        "garbage",
        text_contains("Unknown subcommand"),
    )
    try:
        assert "Unknown subcommand" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "",
        text_contains_any("Current model", "Select model"),
    )
    try:
        assert "model" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_model_set_by_name(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/model sonnet sets the model."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "sonnet",
        text_contains("Model changed"),
    )
    try:
        assert "Model changed" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_model_set_with_effort(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/model sonnet high sets model with effort."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/model",
        "sonnet high",
        text_contains("Model changed"),
    )
    try:
        assert "Model changed" in msg["text"]
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    """/qv on an empty queue shows queue status."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qv",
        "",
        text_contains_any("Queue", "queue", "pending", "items"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qclear",
        "",
        text_contains_any("Cleared", "cleared", "0 pending", "item(s)"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qdelete",
        "",
        text_contains_any("Deleted", "deleted", "item(s)", "0"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /qc subcommands (pause, resume, stop)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_qc_pause(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qc pause requests a queue pause."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qc",
        "pause",
        text_contains_any("pause", "Pause", "paused"),
    )
    try:
        assert "pause" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_qc_resume(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qc resume resumes the queue."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qc",
        "resume",
        text_contains_any("resum", "Resum"),
    )
    try:
        assert "resum" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_qc_stop(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qc stop stops the queue immediately."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qc",
        "stop",
        text_contains_any("stop", "Stop"),
    )
    try:
        assert "stop" in msg["text"].lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_qc_view(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/qc view shows queue status."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/qc",
        "view",
        text_contains_any("Queue", "queue", "pending", "items"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/permissions",
        "",
        text_contains_any("permission", "Permission", "mode", "Mode"),
    )
    try:
        body = msg["text"]
        assert "permission" in body.lower() or "mode" in body.lower()
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/st",
        "",
        text_contains_any("No active jobs", "No active", "job"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /cc (cancel jobs)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_cc_no_jobs(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/cc with no active jobs."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/cc",
        "",
        text_contains_any("Cancelled", "cancelled", "0", "No active"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


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
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/usage",
        "",
        text_contains_any("Claude usage", "Session Status", "Claude Session"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /worktree
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_worktree_list(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/worktree list shows worktrees or 'no worktrees found'."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/worktree",
        "list",
        text_contains_any("worktree", "Worktree", "No worktrees"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_wt_alias(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/wt is an alias for /worktree."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/wt",
        "list",
        text_contains_any("worktree", "Worktree", "No worktrees"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /agents
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_agents_list(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/agents lists available agents."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/agents",
        "",
        text_contains_any("agent", "Agent", "Available"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


@pytest.mark.live
@pytest.mark.asyncio
async def test_agents_create(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/agents create shows creation instructions."""
    msg = await dispatch_and_expect(
        slash_dispatch,
        slack_client,
        slack_test_channel,
        "/agents",
        "create",
        text_contains_any("Create", "create", "agent", "Agent"),
    )
    try:
        assert msg.get("ts")
    finally:
        await delete_message(slack_client, slack_test_channel, msg["ts"])


# ============================================================================
# /mode persistence verification
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_persists_across_show(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/mode bypass followed by /mode shows bypass as current."""
    messages_to_delete: list[str] = []
    try:
        msg1 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/mode",
            "bypass",
            text_contains_any("Mode changed", "bypass"),
        )
        messages_to_delete.append(msg1["ts"])

        msg2 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/mode",
            "",
            text_contains_any("Current mode", "Current permission mode"),
        )
        messages_to_delete.append(msg2["ts"])
        assert "bypass" in msg2["text"].lower()
    finally:
        for ts in messages_to_delete:
            await delete_message(slack_client, slack_test_channel, ts)


# ============================================================================
# /notifications persistence verification
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_notifications_persist(
    slash_dispatch: SlashCommandDispatcher,
    slack_client: AsyncWebClient,
    slack_test_channel: str,
):
    """/notifications off then /notifications shows disabled state."""
    messages_to_delete: list[str] = []
    try:
        msg1 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/notifications",
            "off",
            text_contains("Notifications disabled"),
        )
        messages_to_delete.append(msg1["ts"])

        msg2 = await dispatch_and_expect(
            slash_dispatch,
            slack_client,
            slack_test_channel,
            "/notifications",
            "",
            text_contains_any("Notification settings", "Notification Settings"),
        )
        messages_to_delete.append(msg2["ts"])
        # Both should show off
        assert "off" in msg2["text"].lower()
    finally:
        for ts in messages_to_delete:
            await delete_message(slack_client, slack_test_channel, ts)
