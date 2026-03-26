"""Unit tests for plan approval manager."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.approval.plan_manager import PlanApprovalManager


@pytest.mark.asyncio
async def test_request_approval_uses_default_plan_mention() -> None:
    """Plan approval prompts should include the configured mention prefix."""
    slack_client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
    )

    with patch.object(PlanApprovalManager._pending, "add", new=AsyncMock()), patch.object(
        PlanApprovalManager._pending, "wait_for_result", new=AsyncMock(return_value=True)
    ), patch(
        "src.approval.plan_manager.post_text_snippet",
        new=AsyncMock(),
    ):
        approved = await PlanApprovalManager.request_approval(
            session_id="session-123",
            channel_id="C123",
            plan_content="",
            resume_session_id="resume-123",
            prompt="Plan the work",
            slack_client=slack_client,
        )

    assert approved is True
    slack_client.chat_postMessage.assert_awaited_once()
    assert (
        slack_client.chat_postMessage.await_args.kwargs["text"]
        == "@channel Plan ready for review"
    )


@pytest.mark.asyncio
async def test_request_approval_omits_mention_when_plan_mention_empty() -> None:
    """Empty plan mention config should suppress the mention prefix."""
    slack_client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
    )

    with patch.object(PlanApprovalManager._pending, "add", new=AsyncMock()), patch.object(
        PlanApprovalManager._pending, "wait_for_result", new=AsyncMock(return_value=False)
    ), patch(
        "src.approval.plan_manager.post_text_snippet",
        new=AsyncMock(),
    ), patch(
        "src.approval.plan_manager.config.SLACK_PLAN_MENTION",
        "",
    ):
        approved = await PlanApprovalManager.request_approval(
            session_id="session-123",
            channel_id="C123",
            plan_content="",
            resume_session_id="resume-123",
            prompt="Plan the work",
            slack_client=slack_client,
        )

    assert approved is False
    assert slack_client.chat_postMessage.await_args.kwargs["text"] == "Plan ready for review"
