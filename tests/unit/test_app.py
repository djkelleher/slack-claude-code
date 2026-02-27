"""Unit tests for app-level helpers."""

import asyncio

import pytest

from src.app import slack_api_with_retry


class TestSlackApiRetry:
    """Tests for Slack API retry helper."""

    @pytest.mark.asyncio
    async def test_slack_api_with_retry_propagates_cancellation_immediately(self):
        """CancelledError should never be retried."""
        call_count = 0

        async def failing_call():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await slack_api_with_retry(failing_call, max_retries=3, base_delay=0)

        assert call_count == 1
