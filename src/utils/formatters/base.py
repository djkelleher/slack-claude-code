"""Base formatting utilities and constants."""

import re
from datetime import datetime

from src.config import config

# Re-export for backward compatibility (used by other formatters)
MAX_TEXT_LENGTH = config.SLACK_BLOCK_TEXT_LIMIT
FILE_THRESHOLD = config.SLACK_FILE_THRESHOLD


def escape_markdown(text: str) -> str:
    """Escape special Slack mrkdwn characters.

    Slack's mrkdwn is different from standard Markdown:
    - Bold: *text* (not **text**)
    - Italic: _text_
    - Strike: ~text~
    - Code: `code`
    - Blockquote: > quote
    - Links: <url|text>

    We need to escape & < > which have special meaning in mrkdwn.
    """
    # Order matters: & must be replaced first
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format.

    Main conversions:
    - **bold** -> *bold*
    - __bold__ -> *bold*
    - *italic* -> _italic_
    - _italic_ remains _italic_
    - [text](url) -> <url|text>
    - ```code``` -> ```code``` (code blocks stay the same)
    - `inline` -> `inline` (inline code stays the same)

    Note: In standard Markdown, **text** is bold and *text* is italic.
    In Slack mrkdwn, *text* is bold and _text_ is italic.
    """
    # Protect code blocks and inline code first
    protected_content = []

    # Extract and protect code blocks
    def save_protected(match):
        protected_content.append(match.group(0))
        return f"¤PROTECTED_{len(protected_content)-1}¤"

    # Protect triple-backtick code blocks
    text = re.sub(r"```[\s\S]*?```", save_protected, text)

    # Protect inline code
    text = re.sub(r"`[^`]+`", save_protected, text)

    # Now do the conversions
    # 1. Convert bold: **text** -> *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 2. Convert bold: __text__ -> *text*
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # 3. Convert italic: *text* -> _text_ (but skip the bold ones we just created)
    # Since we've already converted **bold** to *bold*, we need to be careful
    # The remaining single asterisks should be italic markers from the original
    # Actually, let's process this differently - mark all bold first
    parts = []
    i = 0
    while i < len(text):
        # Look for bold markers we just created (*text*)
        if text[i] == "*":
            # Find the closing *
            j = i + 1
            while j < len(text) and text[j] != "*":
                j += 1
            if j < len(text):
                # This is a bold section, keep it as is
                parts.append(text[i : j + 1])
                i = j + 1
                continue
        parts.append(text[i])
        i += 1

    text = "".join(parts)

    # 4. Convert links: [text](url) -> <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"<\2|\1>", text)

    # Don't restore protected content yet - we need to escape first

    # Finally escape special characters (but not in URLs)
    # We need to be careful with escaping since we have <url|text> format
    # Let's protect URLs first
    url_pattern = r"<([^|>]+)\|([^>]+)>"
    urls = []

    def save_url(match):
        urls.append(match.group(0))
        return f"__URL_{len(urls)-1}__"

    text = re.sub(url_pattern, save_url, text)

    # Now escape special characters
    text = escape_markdown(text)

    # Restore URLs
    for i, url in enumerate(urls):
        text = text.replace(f"__URL_{i}__", url)

    # Finally restore protected content (code blocks and inline code)
    for i, content in enumerate(protected_content):
        text = text.replace(f"¤PROTECTED_{i}¤", content)

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
    sanitized = re.sub(r"/home/[^/\s]+", "/home/***", error)
    # Redact common sensitive values
    sanitized = re.sub(
        r'(password|secret|token|key|api_key|apikey|auth)=[^\s&"\']+',
        r"\1=***",
        sanitized,
        flags=re.IGNORECASE,
    )
    # Redact environment variable values that might contain secrets
    sanitized = re.sub(
        r"(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_SIGNING_SECRET)=[^\s]+",
        r"\1=***",
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
        # Find a good break point (newline) near the truncation point
        truncated = output[-max_length + 50 :]
        # Try to start at a newline for cleaner truncation
        newline_pos = truncated.find("\n")
        if newline_pos != -1 and newline_pos < 100:
            truncated = truncated[newline_pos + 1 :]
        return "_... (earlier output truncated)_\n\n" + truncated
    return output
