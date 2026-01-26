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
    _future: Optional[asyncio.Future] = field(default=None, repr=False)
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def future(self) -> asyncio.Future:
        """Lazily create the Future when first accessed in async context."""
        if self._future is None:
            try:
                loop = asyncio.get_running_loop()
                self._future = loop.create_future()
            except RuntimeError:
                # Not in async context - create a new event loop's future
                loop = asyncio.new_event_loop()
                self._future = loop.create_future()
        return self._future


class PlanApprovalManager:
    """Manages pending plan approval requests with Slack integration.

    Uses async futures to block until user responds via Slack buttons.
    Thread-safe via asyncio.Lock for all _pending dictionary access.
    """

    _pending: dict[str, PendingPlanApproval] = {}
    _lock: asyncio.Lock = asyncio.Lock()

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

        async with cls._lock:
            cls._pending[approval_id] = approval

        try:
            # Post approval message to Slack
            if slack_client:
                # First upload the plan as a file snippet (collapsible, downloadable)
                if plan_content:
                    try:
                        filename = os.path.basename(plan_file_path) if plan_file_path else "plan.md"
                        await slack_client.files_upload_v2(
                            channel=channel_id,
                            thread_ts=thread_ts,
                            content=plan_content,
                            filename=filename,
                            filetype="markdown",
                            title=f"Implementation Plan: {filename}",
                        )
                    except Exception as e:
                        logger.warning(f"Failed to upload plan file: {e}")

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
            async with cls._lock:
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
        async with cls._lock:
            approval = cls._pending.get(approval_id)
            if not approval:
                logger.warning(f"Plan approval {approval_id} not found")
                return None

            # Use try-except to handle race condition where another coroutine
            # could resolve the future between our check and set_result
            try:
                approval.future.set_result(approved)
            except asyncio.InvalidStateError:
                logger.warning(f"Plan approval {approval_id} already resolved")
                # Remove from _pending to prevent memory leak
                cls._pending.pop(approval_id, None)
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
        async with cls._lock:
            approval = cls._pending.get(approval_id)
            if not approval:
                return False

            if not approval.future.done():
                approval.future.cancel()

            cls._pending.pop(approval_id, None)
        return True

    @classmethod
    async def cancel_for_session(cls, session_id: str) -> int:
        """Cancel all pending approvals for a session.

        Args:
            session_id: The session ID

        Returns:
            Number of approvals cancelled
        """
        async with cls._lock:
            to_cancel = [aid for aid, a in cls._pending.items() if a.session_id == session_id]

            for approval_id in to_cancel:
                approval = cls._pending.get(approval_id)
                if approval:
                    if not approval.future.done():
                        approval.future.cancel()
                    cls._pending.pop(approval_id, None)

        return len(to_cancel)

    @classmethod
    async def get_pending(cls, session_id: Optional[str] = None) -> list[PendingPlanApproval]:
        """Get pending plan approvals.

        Args:
            session_id: Optional filter by session

        Returns:
            List of pending plan approvals
        """
        async with cls._lock:
            approvals = list(cls._pending.values())
            if session_id:
                approvals = [a for a in approvals if a.session_id == session_id]
        return approvals

    @classmethod
    async def count_pending(cls) -> int:
        """Get count of pending plan approvals."""
        async with cls._lock:
            return len(cls._pending)
