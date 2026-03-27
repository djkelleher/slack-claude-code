"""Live end-to-end tests for DSL directive coverage via Slack.

These tests validate that the running Slack app correctly parses and responds to every
DSL construct: queue-plan block markers, submission directives, mode directives on
regular prompts, combined semicolon directives, and error cases.

Required environment variables:
- SLACK_BOT_TOKEN
- SLACK_USER_TOKEN
- SLACK_TEST_CHANNEL

Run with: pytest tests/integration/test_dsl_live.py --live -v
"""

from datetime import datetime, timedelta
import uuid

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.integration.helpers import (
    MessageCleanup,
    send_and_expect,
    text_contains,
    text_contains_any,
)

# ============================================================================
# Queue Plan — Block Markers
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_prompt_separator(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``***`` prompt separator produces multiple queued items."""
    marker = uuid.uuid4().hex[:8]
    text = f"[DSL {marker}] first prompt\n***\nsecond prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body or "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_prompt_parenthesized(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(prompt)`` is rejected as an unknown queue-plan marker."""
    marker = uuid.uuid4().hex[:8]
    text = f"[DSL {marker}] first prompt\n(prompt)\nsecond prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Unknown queue-plan marker", "Invalid structured queue plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Unknown queue-plan marker" in body or "Invalid structured queue plan" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_branch_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(branch <name>)`` scopes prompts to a named git branch."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(branch test-branch-{marker})\n" f"[DSL {marker}] do something on test branch\n" f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("item(s) from structured plan", "not a git repository", "worktree"),
    )
    try:
        body = bot_msg.get("text", "")
        # Either successful queueing or git error — both confirm DSL routing
        assert "structured" in body or "git" in body.lower() or "worktree" in body.lower()
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_loop_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(loop N)`` repeats enclosed prompts N times."""
    marker = uuid.uuid4().hex[:8]
    text = f"(loop 3)\n" f"[DSL {marker}] repeated prompt\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("3"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "3" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_parallel_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(parallel)`` groups prompts for concurrent execution."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(parallel)\n"
        f"[DSL {marker}] parallel task A\n"
        f"***\n"
        f"[DSL {marker}] parallel task B\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_parallel_with_limit(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(parallel2)`` limits concurrency to 2."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(parallel2)\n"
        f"[DSL {marker}] task A\n"
        f"***\n"
        f"[DSL {marker}] task B\n"
        f"***\n"
        f"[DSL {marker}] task C\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("3"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "3" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_mode_scoped_block(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(mode: bypass)`` scopes mode to enclosed prompts."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: bypass)\n" f"[DSL {marker}] scoped bypass prompt\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("1"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_milestone_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(milestone <name>)`` records a milestone entry in the structured plan."""
    marker = uuid.uuid4().hex[:8]
    text = f"(milestone alpha-{marker})\n[DSL {marker}] milestone prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_usage_limit_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(limit: ...)`` is routed through structured-plan handling."""
    marker = uuid.uuid4().hex[:8]
    text = f"(limit: 2.5% 5h pause)\n[DSL {marker}] limited prompt\n(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any(
            "item(s) from structured plan",
            "usage-limit",
            "not supported yet",
            "Percentage-based queue usage limits",
        ),
    )
    try:
        body = bot_msg.get("text", "")
        assert (
            "structured" in body.lower()
            or "usage-limit" in body.lower()
            or "not supported" in body.lower()
        )
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_end_marker(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(end)`` closes the nearest open block; ``((end))`` is equivalent."""
    marker = uuid.uuid4().hex[:8]
    text = f"(loop 2)\n" f"[DSL {marker}] inside loop\n" f"((end))\n" f"[DSL {marker}] outside loop"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("3"),
    )
    try:
        body = bot_msg.get("text", "")
        # 2 from the loop + 1 after = 3
        assert "item(s) from structured plan" in body
        assert "3" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_for_loop(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``FOR var IN (a, b, c)`` substitution loop expands prompts."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"FOR color IN (red, green, blue)\n" f"[DSL {marker}] paint the wall ((color))\n" f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("3"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "3" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_nested_blocks(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Nested loop + parallel blocks expand correctly."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(loop 2)\n"
        f"(parallel)\n"
        f"[DSL {marker}] nested A\n"
        f"***\n"
        f"[DSL {marker}] nested B\n"
        f"(end)\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("4"),
    )
    try:
        body = bot_msg.get("text", "")
        # 2 iterations × 2 parallel = 4 items
        assert "item(s) from structured plan" in body
        assert "4" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_nested_numeric_loops(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Nested numeric loops expand multiplicatively."""
    marker = uuid.uuid4().hex[:8]
    text = f"(loop 2)\n" f"(loop 3)\n" f"[DSL {marker}] nested numeric prompt\n" f"(end)\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("6"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "6" in body
    finally:
        await cleanup.cleanup()


# ============================================================================
# Queue Plan — Submission Directives
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_append(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(append)`` appends items to the pending queue."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(append)\n"
        f"[DSL {marker}] appended prompt A\n"
        f"***\n"
        f"[DSL {marker}] appended prompt B"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Added" in body or "item(s)" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_prepend(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(prepend)`` inserts items at the front of the queue."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(prepend)\n"
        f"[DSL {marker}] prepended prompt\n"
        f"***\n"
        f"[DSL {marker}] prepended prompt 2"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Prepended" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_insert_at_index(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(insert1)`` inserts items at queue position 1."""
    marker = uuid.uuid4().hex[:8]
    text = f"(insert1)\n" f"[DSL {marker}] inserted prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Inserted" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_auto(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(auto)`` enables auto-follow checks after each prompt."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(auto)\n"
        f"[DSL {marker}] auto-follow prompt A\n"
        f"***\n"
        f"[DSL {marker}] auto-follow prompt B"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Auto checks" in body or "auto" in body.lower()
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_auto_finish(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(auto-finish)`` enables consolidated auto-follow when the queue drains."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(auto-finish)\n"
        f"[DSL {marker}] auto-finish prompt A\n"
        f"***\n"
        f"[DSL {marker}] auto-finish prompt B"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "auto-finish" in body.lower() or "Auto" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_combined_prepend_auto(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(prepend, auto)`` combines submission and automation directives."""
    marker = uuid.uuid4().hex[:8]
    text = f"(prepend, auto)\n" f"[DSL {marker}] combined directive prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Prepended" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_submission_timer_directive(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(at HH:MM pause)`` persists scheduled controls in the queue flow."""
    marker = uuid.uuid4().hex[:8]
    hhmm = (datetime.now().astimezone() + timedelta(minutes=10)).strftime("%H:%M")
    text = f"(at {hhmm} pause)\n[DSL {marker}] scheduled prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Scheduled controls:", "scheduled", "pause"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Scheduled controls:" in body or "scheduled" in body.lower()
    finally:
        await cleanup.cleanup()


# ============================================================================
# Mode Directives — Regular Prompts (non-queue-plan)
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_bypass(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: bypass)`` on a single prompt applies bypass mode for that execution."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: bypass)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        # Bot should respond — the mode was applied and prompt executed
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_accept(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: accept)`` applies acceptEdits permission mode."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: accept)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_plan(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: plan)`` puts execution into plan mode."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: plan)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_ask(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: ask)`` is an alias for default mode."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: ask)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_default(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: default)`` explicitly uses default mode."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: default)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_delegate(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: delegate)`` uses delegated permission mode."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: delegate)\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_with_end_marker(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``(mode: bypass)`` with explicit ``(end)`` on a single prompt."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: bypass)\n[DSL {marker}] say hello\n(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_mode_directive_double_paren_syntax(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """``((mode: bypass))`` double-paren syntax is equivalent to single."""
    marker = uuid.uuid4().hex[:8]
    text = f"((mode: bypass))\n[DSL {marker}] say hello"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        lambda msg: msg.get("user") == slack_bot_user_id,
        timeout_seconds=90,
    )
    try:
        assert bot_msg.get("ts")
    finally:
        await cleanup.cleanup()


# ============================================================================
# Combined Mode Directives — Semicolons and Plan Strategies
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_mode_with_semicolons(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Semicolons in ``(mode: ...)`` separate multiple sub-directives in queue DSL."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(mode: plan; ask)\n"
        f"[DSL {marker}] prompt with semicolon mode scope\n"
        f"***\n"
        f"[DSL {marker}] second prompt\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_queue_plan_mode_plan_in_scope(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(mode: plan)`` scoped block in queue plan carries plan mode for enclosed items."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(mode: plan)\n"
        f"[DSL {marker}] plan mode prompt\n"
        f"(end)\n"
        f"[DSL {marker}] default mode prompt"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
    finally:
        await cleanup.cleanup()


# ============================================================================
# Error Cases
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_invalid_mode_value(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Invalid mode value produces a clear error message."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: notamode)\n[DSL {marker}] should fail"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Invalid mode", "Unknown mode"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "mode" in body.lower()
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_empty_mode_value(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
    slack_bot_user_id: str,
):
    """Empty mode directive value is rejected."""
    marker = uuid.uuid4().hex[:8]
    text = f"(mode: )\n[DSL {marker}] should fail"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Invalid mode", "must include"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "mode" in body.lower()
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_unknown_queue_marker(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Unknown parenthesized queue marker produces a parse error."""
    marker = uuid.uuid4().hex[:8]
    text = f"(nonsense_marker)\n[DSL {marker}] should fail"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Unknown queue-plan marker", "Invalid structured queue plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "queue" in body.lower() or "marker" in body.lower() or "Invalid" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_unmatched_end_marker(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Standalone ``(end)`` without a matching open block produces an error."""
    marker = uuid.uuid4().hex[:8]
    text = f"[DSL {marker}] prompt text\n(end)\n***\nsecond prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("end marker", "Invalid structured queue plan", "without a matching"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "end" in body.lower() or "Invalid" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_insert_directive_zero_index(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(insert0)`` is rejected because indices are 1-based."""
    marker = uuid.uuid4().hex[:8]
    text = f"(insert0)\n[DSL {marker}] should fail"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Insert directives", "insert", "Invalid"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "insert" in body.lower() or "Invalid" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_conflicting_submission_directives(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Conflicting ``(append)`` and ``(prepend)`` produces an error."""
    marker = uuid.uuid4().hex[:8]
    text = f"(append)\n" f"(prepend)\n" f"[DSL {marker}] should fail\n" f"***\n" f"second prompt"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("conflict", "Invalid structured queue plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "conflict" in body.lower() or "Invalid" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_error_for_loop_empty_values(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``FOR x IN ()`` with empty values is rejected."""
    marker = uuid.uuid4().hex[:8]
    text = f"FOR x IN ()\n[DSL {marker}] should fail"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains_any("Invalid", "substitution loop", "at least one value"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Invalid" in body or "substitution" in body.lower()
    finally:
        await cleanup.cleanup()


# ============================================================================
# Complex / Combined Scenarios
# ============================================================================


@pytest.mark.live
@pytest.mark.asyncio
async def test_combined_loop_for_parallel(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """FOR loop inside a parallel block expands correctly."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(parallel)\n"
        f"FOR lang IN (python, rust)\n"
        f"[DSL {marker}] lint ((lang)) files\n"
        f"(end)\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_combined_submission_and_block_directives(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Submission directive + block directives work together."""
    marker = uuid.uuid4().hex[:8]
    text = f"(prepend)\n" f"(loop 2)\n" f"[DSL {marker}] looped prepended prompt\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("item(s) from structured plan"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "Prepended" in body
        assert "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_combined_mode_scope_with_submission(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(append)`` + ``(mode: bypass)`` scoped block."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(append)\n"
        f"(mode: bypass)\n"
        f"[DSL {marker}] bypass scoped A\n"
        f"***\n"
        f"[DSL {marker}] bypass scoped B\n"
        f"(end)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_inline_prompt_after_block_marker(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """Inline prompt text after a block marker is captured as the first prompt."""
    marker = uuid.uuid4().hex[:8]
    text = f"(loop 2) [DSL {marker}] inline prompt\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "2" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_for_loop_with_variable_in_prompt(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """FOR loop variable substitution with single-paren ``(var)`` syntax."""
    marker = uuid.uuid4().hex[:8]
    text = f"FOR svc IN (api, web, worker)\n" f"[DSL {marker}] restart the (svc) service\n" f"(end)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("3"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
        assert "3" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_end_with_count(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(end2)`` closes two nested blocks at once."""
    marker = uuid.uuid4().hex[:8]
    text = f"(loop 2)\n" f"(parallel)\n" f"[DSL {marker}] deep nested prompt\n" f"(end2)"
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("2"),
    )
    try:
        body = bot_msg.get("text", "")
        assert "item(s) from structured plan" in body
    finally:
        await cleanup.cleanup()


@pytest.mark.live
@pytest.mark.asyncio
async def test_combined_block_directive_line(
    slack_client: AsyncWebClient,
    slack_user_client: AsyncWebClient,
    slack_test_channel: str,
):
    """``(loop 2, parallel)`` combined on one line expands to nested blocks."""
    marker = uuid.uuid4().hex[:8]
    text = (
        f"(loop 2, parallel)\n"
        f"[DSL {marker}] combined directive prompt A\n"
        f"***\n"
        f"[DSL {marker}] combined directive prompt B\n"
        f"(end2)"
    )
    bot_msg, cleanup = await send_and_expect(
        slack_client,
        slack_user_client,
        slack_test_channel,
        text,
        text_contains("4"),
    )
    try:
        body = bot_msg.get("text", "")
        # 2 iterations × 2 parallel items = 4
        assert "item(s) from structured plan" in body
        assert "4" in body
    finally:
        await cleanup.cleanup()
