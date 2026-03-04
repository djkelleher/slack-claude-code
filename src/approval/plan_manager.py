"""Plan approval manager for Slack integration.

Manages pending plan approval requests with async futures for approval responses.
Similar to PermissionManager but specialized for plan mode workflow.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from src.utils.pending_manager import PendingManager
from src.utils.slack_helpers import post_text_snippet, sanitize_snippet_content

from .slack_ui import build_plan_approval_blocks


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


class PlanApprovalManager:
    """Manages pending plan approval requests with Slack integration.

    Uses async futures to block until user responds via Slack buttons.
    Thread-safe via asyncio.Lock for all _pending dictionary access.
    """

    _pending = PendingManager[PendingPlanApproval]()

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
        plan_file_path: Optional[str] = None,
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
            plan_file_path: Optional path to plan markdown file for attachment

        Returns:
            True if approved, False if denied
        """
        approval_id = str(uuid.uuid4())[:8]
        future = asyncio.get_running_loop().create_future()

        approval = PendingPlanApproval(
            approval_id=approval_id,
            session_id=session_id,
            channel_id=channel_id,
            plan_content=plan_content,
            claude_session_id=claude_session_id,
            prompt=prompt,
            user_id=user_id,
            thread_ts=thread_ts,
            future=future,
        )

        await cls._pending.add(approval_id, approval)

        try:
            # Post approval message to Slack
            if slack_client:
                # Post the plan as an inline message (avoids binary file issues)
                if plan_content:
                    try:
                        filename = os.path.basename(plan_file_path) if plan_file_path else "plan.md"
                        await post_text_snippet(
                            client=slack_client,
                            channel_id=channel_id,
                            content=sanitize_snippet_content(plan_content),
                            title=f"Implementation Plan: {filename}",
                            thread_ts=thread_ts,
                            format_as_text=True,
                            render_tables=True,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to post plan snippet: {e}")

                # Then post the approval buttons
                blocks = build_plan_approval_blocks(
                    approval_id=approval_id,
                    session_id=session_id,
                )

                result = await slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=blocks,
                    text="Plan ready for review",
                )

                approval.message_ts = result.get("ts")

            # Wait for response indefinitely
            approved = await approval.future
            return approved

        except asyncio.CancelledError:
            logger.info(f"Plan approval {approval_id} was cancelled")
            return False

        finally:
            await cls._pending.pop(approval_id)

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
        approval = await cls._pending.resolve(approval_id, approved)
        if not approval:
            logger.warning(f"Plan approval {approval_id} not found or already resolved")
            return None

        logger.info(
            f"Plan approval {approval_id} {'approved' if approved else 'denied'} "
            f"by {resolved_by or 'unknown'}"
        )

        return approval

    @classmethod
    async def cancel(cls, approval_id: str) -> bool:
        """Cancel a pending plan approval request.

        Args:
            approval_id: The approval ID to cancel

        Returns:
            True if approval was found and cancelled
        """
        return await cls._pending.cancel(approval_id)

    @classmethod
    async def cancel_for_session(cls, session_id: str) -> int:
        """Cancel all pending approvals for a session.

        Args:
            session_id: The session ID

        Returns:
            Number of approvals cancelled
        """
        return await cls._pending.cancel_for_session(session_id)

    @classmethod
    async def get_pending(cls, session_id: Optional[str] = None) -> list[PendingPlanApproval]:
        """Get pending plan approvals.

        Args:
            session_id: Optional filter by session

        Returns:
            List of pending plan approvals
        """
        return await cls._pending.list(session_id=session_id)

    @classmethod
    async def count_pending(cls) -> int:
        """Get count of pending plan approvals."""
        return await cls._pending.count()
