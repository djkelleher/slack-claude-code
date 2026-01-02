"""Usage checker for Claude Code Pro plan.

Runs `claude usage` CLI command to check current usage percentage.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class UsageSnapshot:
    """Snapshot of current usage."""

    usage_percent: float
    reset_time: Optional[datetime] = None
    is_paused: bool = False
    checked_at: datetime = None

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.now()


class UsageChecker:
    """Check Claude Code Pro plan usage via CLI.

    Caches results to avoid excessive CLI calls.
    """

    def __init__(self, cache_duration: int = None) -> None:
        """Initialize usage checker.

        Args:
            cache_duration: How long to cache usage results (seconds).
                           Defaults to config.timeouts.cache.usage.
        """
        self.cache_duration = cache_duration or config.timeouts.cache.usage
        self._cache: Optional[UsageSnapshot] = None
        self._cache_time: Optional[datetime] = None

    def _cache_valid(self) -> bool:
        """Check if cached value is still valid."""
        if self._cache is None or self._cache_time is None:
            return False

        elapsed = (datetime.now() - self._cache_time).total_seconds()
        return elapsed < self.cache_duration

    async def get_usage(self, force_refresh: bool = False) -> UsageSnapshot:
        """Get current usage percentage.

        Args:
            force_refresh: Bypass cache and get fresh data

        Returns:
            UsageSnapshot with current usage
        """
        if not force_refresh and self._cache_valid():
            return self._cache

        # Run claude usage command
        output = await self._execute_command()

        # Parse the output
        usage_percent = self._parse_usage(output)
        reset_time = self._parse_reset_time(output)

        snapshot = UsageSnapshot(
            usage_percent=usage_percent,
            reset_time=reset_time,
        )

        # Update cache
        self._cache = snapshot
        self._cache_time = datetime.now()

        return snapshot

    async def check_should_pause(self, threshold: float = 85.0) -> Tuple[bool, Optional[datetime]]:
        """Check if usage exceeds threshold.

        Args:
            threshold: Usage percentage threshold

        Returns:
            Tuple of (should_pause, reset_time)
        """
        snapshot = await self.get_usage()
        should_pause = snapshot.usage_percent >= threshold
        return should_pause, snapshot.reset_time

    async def _execute_command(self) -> str:
        """Execute claude usage command and return output."""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "usage",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=config.timeouts.execution.usage_check,
            )

            output = stdout.decode("utf-8", errors="replace")
            if not output and stderr:
                output = stderr.decode("utf-8", errors="replace")

            return output

        except asyncio.TimeoutError:
            logger.error("Usage check timed out")
            return ""
        except Exception as e:
            logger.error(f"Failed to check usage: {e}")
            return ""

    def _parse_usage(self, output: str) -> float:
        """Parse usage percentage from output.

        Looks for patterns like:
        - "45.2% used"
        - "Usage: 45.2%"
        - "45.2/100"
        """
        if not output:
            return 0.0

        # Try different patterns
        patterns = [
            r"(\d+(?:\.\d+)?)\s*%\s*(?:used|of)",
            r"Usage[:\s]+(\d+(?:\.\d+)?)\s*%",
            r"(\d+(?:\.\d+)?)\s*/\s*100",
            r"(\d+(?:\.\d+)?)\s*percent",
        ]

        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue

        # Fallback: look for any percentage
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", output)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        logger.warning(f"Could not parse usage from: {output[:100]}")
        # Return 100% to be safe - assume near limit when parsing fails
        # This prevents bypassing budget limits due to parsing errors
        return 100.0

    def _parse_reset_time(self, output: str) -> Optional[datetime]:
        """Parse reset time from output.

        Looks for patterns like:
        - "Resets in 5 hours"
        - "Resets at 2024-01-15 00:00"
        - "Next reset: tomorrow"
        """
        if not output:
            return None

        # Try to find reset time patterns
        patterns = [
            r"[Rr]esets?\s+(?:in\s+)?(\d+)\s*(?:hours?|hrs?)",
            r"[Rr]esets?\s+(?:in\s+)?(\d+)\s*(?:minutes?|mins?)",
            r"[Rr]esets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2})",
        ]

        now = datetime.now()

        # Check for "in X hours"
        match = re.search(r"[Rr]esets?\s+(?:in\s+)?(\d+)\s*(?:hours?|hrs?)", output)
        if match:
            hours = int(match.group(1))
            return now.replace(minute=0, second=0, microsecond=0) + \
                   __import__('datetime').timedelta(hours=hours)

        # Check for "in X minutes"
        match = re.search(r"[Rr]esets?\s+(?:in\s+)?(\d+)\s*(?:minutes?|mins?)", output)
        if match:
            minutes = int(match.group(1))
            return now + __import__('datetime').timedelta(minutes=minutes)

        # Check for specific date
        match = re.search(r"(\d{4}-\d{2}-\d{2})", output)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d")
            except ValueError:
                pass

        return None

    def invalidate_cache(self) -> None:
        """Invalidate the cached usage data."""
        self._cache = None
        self._cache_time = None
