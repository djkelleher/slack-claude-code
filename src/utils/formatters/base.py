"""Base formatting utilities and constants."""

import re
from datetime import datetime


# Constants
MAX_TEXT_LENGTH = 2900  # Leave room for formatting
FILE_THRESHOLD = 2000  # Attach as file if longer than this


def escape_markdown(text: str) -> str:
    """Escape special Slack markdown characters."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def time_ago(dt: datetime) -> str:
    """Format a datetime as 'X time ago'."""
    now = datetime.now()
    diff = now - dt

    seconds = diff.total_seconds()
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} min{'s' if mins != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"


def sanitize_error(error: str) -> str:
    """Sanitize error message to remove sensitive information."""
    # Redact home directory paths
    sanitized = re.sub(r'/home/[^/\s]+', '/home/***', error)
    # Redact common sensitive values
    sanitized = re.sub(
        r'(password|secret|token|key|api_key|apikey|auth)=[^\s&"\']+',
        r'\1=***',
        sanitized,
        flags=re.IGNORECASE,
    )
    # Redact environment variable values that might contain secrets
    sanitized = re.sub(
        r'(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_SIGNING_SECRET|DATABASE_PATH)=[^\s]+',
        r'\1=***',
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized[:2500]


def truncate_output(output: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Truncate output to max length with indicator."""
    if len(output) > max_length:
        return output[: max_length - 50] + "\n\n... (output truncated)"
    return output


def truncate_from_start(output: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Truncate output from start (for streaming where recent content matters)."""
    if len(output) > max_length:
        return "... (earlier output truncated)\n\n" + output[-max_length + 50:]
    return output
