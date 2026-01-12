"""Budget scheduler with time-aware thresholds.

Provides different usage thresholds for night vs day hours.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from ..config import config


@dataclass
class BudgetThresholds:
    """Budget thresholds configuration."""

    day_threshold: float = 85.0
    night_threshold: float = 95.0
    night_start_hour: int = 22
    night_end_hour: int = 6


class BudgetScheduler:
    """Manages time-based budget thresholds.

    During night hours, allows higher usage (more aggressive work).
    During day hours, preserves capacity for interactive use.
    """

    def __init__(
        self,
        day_threshold: Optional[float] = None,
        night_threshold: Optional[float] = None,
        night_start: Optional[int] = None,
        night_end: Optional[int] = None,
    ) -> None:
        """Initialize scheduler with thresholds.

        Args:
            day_threshold: Usage threshold during day hours
            night_threshold: Usage threshold during night hours
            night_start: Hour when night starts (0-23)
            night_end: Hour when night ends (0-23)
        """
        self.thresholds = BudgetThresholds(
            day_threshold=day_threshold or config.USAGE_THRESHOLD_DAY,
            night_threshold=night_threshold or config.USAGE_THRESHOLD_NIGHT,
            night_start_hour=night_start or config.NIGHT_START_HOUR,
            night_end_hour=night_end or config.NIGHT_END_HOUR,
        )

    def is_nighttime(self, dt: Optional[datetime] = None) -> bool:
        """Check if current time is within night hours.

        Args:
            dt: Optional datetime to check (defaults to now)

        Returns:
            True if within night hours
        """
        if dt is None:
            dt = datetime.now()

        hour = dt.hour
        start = self.thresholds.night_start_hour
        end = self.thresholds.night_end_hour

        # Handle wraparound (e.g., 22:00 to 06:00)
        if start > end:
            return hour >= start or hour < end
        else:
            return start <= hour < end

    def get_current_threshold(self, dt: Optional[datetime] = None) -> float:
        """Get the appropriate threshold for current time.

        Args:
            dt: Optional datetime to check (defaults to now)

        Returns:
            Usage percentage threshold
        """
        if self.is_nighttime(dt):
            return self.thresholds.night_threshold
        return self.thresholds.day_threshold

    def get_schedule_info(self, dt: Optional[datetime] = None) -> dict:
        """Get current schedule information.

        Args:
            dt: Optional datetime to check (defaults to now)

        Returns:
            Dict with schedule details
        """
        if dt is None:
            dt = datetime.now()

        is_night = self.is_nighttime(dt)
        threshold = self.get_current_threshold(dt)

        return {
            "current_hour": dt.hour,
            "is_nighttime": is_night,
            "current_threshold": threshold,
            "day_threshold": self.thresholds.day_threshold,
            "night_threshold": self.thresholds.night_threshold,
            "night_start": self.thresholds.night_start_hour,
            "night_end": self.thresholds.night_end_hour,
        }

    def should_pause_for_usage(
        self, usage_percent: float, dt: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """Check if work should pause based on usage and time.

        Args:
            usage_percent: Current usage percentage
            dt: Optional datetime to check

        Returns:
            Tuple of (should_pause, reason)
        """
        threshold = self.get_current_threshold(dt)

        if usage_percent >= threshold:
            period = "night" if self.is_nighttime(dt) else "day"
            reason = f"Usage {usage_percent:.1f}% exceeds {period} threshold of {threshold}%"
            return True, reason

        return False, ""

    def get_time_until_threshold_change(self, dt: Optional[datetime] = None) -> int:
        """Get minutes until threshold changes.

        Args:
            dt: Optional datetime to check

        Returns:
            Minutes until threshold changes (night <-> day)
        """
        if dt is None:
            dt = datetime.now()

        current_hour = dt.hour
        current_minute = dt.minute

        if self.is_nighttime(dt):
            # Find minutes until night ends
            target = self.thresholds.night_end_hour
            if current_hour >= self.thresholds.night_start_hour:
                # Past midnight case
                hours_until = (24 - current_hour) + target
            else:
                hours_until = target - current_hour
        else:
            # Find minutes until night starts
            target = self.thresholds.night_start_hour
            hours_until = target - current_hour

        return max(0, (hours_until * 60) - current_minute)
