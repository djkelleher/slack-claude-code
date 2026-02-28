"""Permission approval handler for Slack integration.

Manages pending permission requests with async futures for approval responses.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from ..database.repository import DatabaseRepository
from ..hooks.registry import HookRegistry, create_context
from ..hooks.types import HookEvent, HookEventType
from ..utils.pending_manager import PendingManager
from .slack_ui import build_approval_blocks


async def _post_permission_notification(
    slack_client: AsyncWebClient,
    channel_id: str,
    thread_ts: Optional[str],
    db: Optional[DatabaseRepository] = None,
) -> None:
    """Post channel notification for permission request."""
    try:
        # Check settings
        if db:
            settings = await db.get_notification_settings(channel_id)
            if not settings.notify_on_permission:
                return

        # Build thread link
        if thread_ts:
            thread_link = (
                f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
            )
            message = f"⚠️ Claude needs permission • <{thread_link}|Respond in thread>"
        else:
            message = "⚠️ Claude needs permission"

        # Post to channel (NOT thread) - triggers sound + unread badge
        await slack_client.chat_postMessage(
            channel=channel_id,
            text=message,
        )
        logger.debug(f"Posted permission notification to channel {channel_id}")

    except asyncio.CancelledError:
        # Don't log cancellation as an error
        pass
    except Exception as e:
        # Log with type for debugging unexpected errors
        logger.warning(
            f"Failed to post permission notification: {type(e).__name__}: {e}"
        )


@dataclass
class PendingApproval:
    """A pending permission approval request."""

    approval_id: str
    session_id: str
    channel_id: str
    tool_name: str
    tool_input: Optional[str] = None
    user_id: Optional[str] = None
    thread_ts: Optional[str] = None
    message_ts: Optional[str] = None
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    created_at: datetime = field(default_factory=datetime.now)


class PermissionManager:
    """Manages pending permission requests with Slack integration.

    Uses async futures to block until user responds via Slack buttons.
    Thread-safe via asyncio.Lock for all _pending dictionary access.
    """

    _pending = PendingManager[PendingApproval]()

    @classmethod
    async def request_approval(
        cls,
        session_id: str,
        channel_id: str,
        tool_name: str,
        tool_input: Optional[str] = None,
        user_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        slack_client: Optional[AsyncWebClient] = None,
        db: Optional[DatabaseRepository] = None,
        auto_approve_tools: list[str] = None,
    ) -> bool:
        """Request approval via Slack and wait for response.

        Waits indefinitely until the user responds via Slack buttons.

        Args:
            session_id: The session requesting approval
            channel_id: Slack channel to post approval request
            tool_name: Name of the tool requesting permission
            tool_input: Optional input/arguments for the tool
            user_id: Optional user who initiated the request
            thread_ts: Optional thread to post in
            slack_client: Slack client for posting message
            db: Optional database repository for notification settings
            auto_approve_tools: List of tool names to auto-approve

        Returns:
            True if approved, False if denied or cancelled
        """
        # Check if tool is in auto-approve list
        if auto_approve_tools and tool_name in auto_approve_tools:
            logger.info(f"Auto-approving tool: {tool_name}")
            return True

        approval_id = str(uuid.uuid4())[:8]
        future = asyncio.get_running_loop().create_future()

        approval = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            channel_id=channel_id,
            tool_name=tool_name,
            tool_input=tool_input,
            user_id=user_id,
            thread_ts=thread_ts,
            future=future,
        )

        await cls._pending.add(approval_id, approval)

        # Emit APPROVAL_NEEDED hook
        await HookRegistry.emit(
            HookEvent(
                event_type=HookEventType.APPROVAL_NEEDED,
                context=create_context(
                    session_id=session_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    user_id=user_id,
                ),
                data={
                    "approval_id": approval_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
            )
        )

        try:
            # Post approval message to Slack
            if slack_client:
                blocks = build_approval_blocks(
                    approval_id=approval_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    session_id=session_id,
                )

                result = await slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=blocks,
                    text=f"Permission requested: {tool_name}",
                )

                approval.message_ts = result.get("ts")

                # Post channel notification (triggers sound + unread badge)
                await _post_permission_notification(
                    slack_client, channel_id, thread_ts, db
                )

            # Wait for response indefinitely (no timeout)
            approved = await approval.future
            return approved

        except asyncio.CancelledError:
            logger.info(f"Approval {approval_id} was cancelled")
            return False

        finally:
            await cls._pending.pop(approval_id)

    @classmethod
    async def resolve(
        cls,
        approval_id: str,
        approved: bool,
        resolved_by: Optional[str] = None,
    ) -> Optional[PendingApproval]:
        """Resolve a pending approval request.

        Called when user clicks approve/deny button in Slack.

        Args:
            approval_id: The approval ID to resolve
            approved: True if approved, False if denied
            resolved_by: Optional user ID who resolved

        Returns:
            The PendingApproval if found and resolved, None otherwise
        """
        approval = await cls._pending.resolve(approval_id, approved)
        if not approval:
            logger.warning(f"Approval {approval_id} not found or already resolved")
            return None

        logger.info(
            f"Approval {approval_id} {'approved' if approved else 'denied'} "
            f"by {resolved_by or 'unknown'}"
        )

        # Emit APPROVAL_RESPONSE hook
        await HookRegistry.emit(
            HookEvent(
                event_type=HookEventType.APPROVAL_RESPONSE,
                context=create_context(
                    session_id=approval.session_id,
                    channel_id=approval.channel_id,
                    thread_ts=approval.thread_ts,
                    user_id=resolved_by,
                ),
                data={
                    "approval_id": approval_id,
                    "tool_name": approval.tool_name,
                    "approved": approved,
                },
            )
        )

        return approval

    @classmethod
    async def cancel(cls, approval_id: str) -> bool:
        """Cancel a pending approval request.

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
    async def get_pending(
        cls, session_id: Optional[str] = None
    ) -> list[PendingApproval]:
        """Get pending approvals.

        Args:
            session_id: Optional filter by session

        Returns:
            List of pending approvals
        """
        return await cls._pending.list(session_id=session_id)

    @classmethod
    async def count_pending(cls) -> int:
        """Get count of pending approvals."""
        return await cls._pending.count()
