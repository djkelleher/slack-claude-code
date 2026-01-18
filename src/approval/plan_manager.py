"""Plan approval manager for Slack integration.

Manages pending plan approval requests with async futures for approval responses.
Similar to PermissionManager but specialized for plan mode workflow.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from ..config import config


@dataclass
class PendingPlanApproval:
    """A pending plan approval request."""

    approval_id: str
    session_id: str
    channel_id: str
    plan_content: str
    claude_session_id: str  # For --resume
    prompt: str  # Original prompt
    user_id: Optional[str] = None
    thread_ts: Optional[str] = None
    message_ts: Optional[str] = None
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    created_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.future is None:
            loop = asyncio.get_running_loop()
            self.future = loop.create_future()


class PlanApprovalManager:
    """Manages pending plan approval requests with Slack integration.

    Uses async futures to block until user responds via Slack buttons.
    """

    _pending: dict[str, PendingPlanApproval] = {}

    @classmethod
    async def request_approval(
        cls,
        session_id: str,
        channel_id: str,
        plan_content: str,
        claude_session_id: str,
        prompt: str,
        user_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        slack_client: Optional[AsyncWebClient] = None,
        timeout: int = None,
    ) -> bool:
        """Request plan approval via Slack and wait for response.

        Args:
            session_id: The session requesting approval
            channel_id: Slack channel to post approval request
            plan_content: The plan text to show user
            claude_session_id: Claude session ID for --resume
            prompt: Original prompt
            user_id: Optional user who initiated the request
            thread_ts: Optional thread to post in
            slack_client: Slack client for posting message
            timeout: Timeout in seconds (defaults to config)

        Returns:
            True if approved, False if denied or timed out
        """
        if timeout is None:
            timeout = config.timeouts.execution.plan_approval

        approval_id = str(uuid.uuid4())[:8]

        approval = PendingPlanApproval(
            approval_id=approval_id,
            session_id=session_id,
            channel_id=channel_id,
            plan_content=plan_content,
            claude_session_id=claude_session_id,
            prompt=prompt,
            user_id=user_id,
            thread_ts=thread_ts,
        )

        cls._pending[approval_id] = approval

        try:
            # Post approval message to Slack
            if slack_client:
                from .slack_ui import build_plan_approval_blocks

                blocks = build_plan_approval_blocks(
                    approval_id=approval_id,
                    plan_content=plan_content,
                    session_id=session_id,
                )

                result = await slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=blocks,
                    text="Plan ready for review",
                )

                approval.message_ts = result.get("ts")

            # Wait for response with timeout
            approved = await asyncio.wait_for(
                approval.future,
                timeout=timeout,
            )

            return approved

        except asyncio.TimeoutError:
            logger.info(f"Plan approval {approval_id} timed out after {timeout}s")
            return False

        except asyncio.CancelledError:
            logger.info(f"Plan approval {approval_id} was cancelled")
            return False

        finally:
            cls._pending.pop(approval_id, None)

    @classmethod
    async def resolve(
        cls,
        approval_id: str,
        approved: bool,
        resolved_by: Optional[str] = None,
    ) -> Optional[PendingPlanApproval]:
        """Resolve a pending plan approval request.

        Called when user clicks approve/deny button in Slack.

        Args:
            approval_id: The approval ID to resolve
            approved: True if approved, False if denied
            resolved_by: Optional user ID who resolved

        Returns:
            The PendingPlanApproval if found and resolved, None otherwise
        """
        approval = cls._pending.get(approval_id)
        if not approval:
            logger.warning(f"Plan approval {approval_id} not found")
            return None

        if approval.future.done():
            logger.warning(f"Plan approval {approval_id} already resolved")
            return None

        approval.future.set_result(approved)
        logger.info(
            f"Plan approval {approval_id} {'approved' if approved else 'denied'} "
            f"by {resolved_by or 'unknown'}"
        )

        return approval

    @classmethod
    def cancel(cls, approval_id: str) -> bool:
        """Cancel a pending plan approval request.

        Args:
            approval_id: The approval ID to cancel

        Returns:
            True if approval was found and cancelled
        """
        approval = cls._pending.get(approval_id)
        if not approval:
            return False

        if not approval.future.done():
            approval.future.cancel()

        cls._pending.pop(approval_id, None)
        return True

    @classmethod
    def cancel_for_session(cls, session_id: str) -> int:
        """Cancel all pending approvals for a session.

        Args:
            session_id: The session ID

        Returns:
            Number of approvals cancelled
        """
        to_cancel = [aid for aid, a in cls._pending.items() if a.session_id == session_id]

        for approval_id in to_cancel:
            cls.cancel(approval_id)

        return len(to_cancel)

    @classmethod
    def get_pending(cls, session_id: Optional[str] = None) -> list[PendingPlanApproval]:
        """Get pending plan approvals.

        Args:
            session_id: Optional filter by session

        Returns:
            List of pending plan approvals
        """
        approvals = list(cls._pending.values())
        if session_id:
            approvals = [a for a in approvals if a.session_id == session_id]
        return approvals

    @classmethod
    def count_pending(cls) -> int:
        """Get count of pending plan approvals."""
        return len(cls._pending)
