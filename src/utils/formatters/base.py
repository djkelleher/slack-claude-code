"""Base formatting utilities and constants."""

import re
from datetime import datetime

from src.config import config

# Re-export for backward compatibility (used by other formatters)
MAX_TEXT_LENGTH = config.SLACK_BLOCK_TEXT_LIMIT
FILE_THRESHOLD = config.SLACK_FILE_THRESHOLD


def flatten_text(text: str) -> str:
    """Flatten paragraph text while preserving markdown structure.

    Joins consecutive paragraph lines into single lines for better Slack display,
    but preserves structure for:
    - Code blocks (triple backticks)
    - Tables (lines with | characters)
    - Headers (lines starting with #)
    - List items (lines starting with -, *, or numbers)
    - Blank lines (paragraph separators)

    Parameters
    ----------
    text : str
        The text to flatten.

    Returns
    -------
    str
        Text with paragraph lines joined, but structure preserved.
    """
    if not text:
        return text

    # Protect code blocks by extracting them first
    code_blocks = []

    def save_code_block(match: re.Match) -> str:
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", save_code_block, text)

    # Tables are now handled separately by table.py - just preserve them as-is
    # A table is a sequence of lines containing | characters
    tables = []

    def save_table(match: re.Match) -> str:
        # Keep table content as-is (will be converted to Slack table blocks later)
        table_content = match.group(1).strip()
        tables.append(table_content)
        return f"\x00TABLE{len(tables) - 1}\x00"

    # Match consecutive lines that look like table rows (contain |)
    # Table pattern: lines starting with | or containing | at least twice
    text = re.sub(
        r"(?:^|\n)((?:[ \t]*\|[^\n]*\|[^\n]*\n?)+)",
        lambda m: "\n" + save_table(m),
        text,
    )

    # Process line by line to preserve structure
    lines = text.split("\n")
    result_lines = []
    current_paragraph = []

    def flush_paragraph():
        if current_paragraph:
            # Join paragraph lines with spaces
            result_lines.append(" ".join(current_paragraph))
            current_paragraph.clear()

    for line in lines:
        stripped = line.strip()

        # Check if this is a structural line that should be preserved
        is_structural = (
            not stripped  # Blank line (paragraph separator)
            or stripped.startswith("#")  # Header
            or stripped.startswith("- ")  # Bullet list
            or stripped.startswith("* ")  # Bullet list
            or stripped.startswith("• ")  # Already converted bullet
            or re.match(r"^\d+\.\s", stripped)  # Numbered list
            or stripped.startswith(">")  # Blockquote
            or stripped.startswith("\x00")  # Protected content placeholder
            or stripped.startswith("---")  # Horizontal rule
            or stripped.startswith("***")  # Horizontal rule
        )

        if is_structural:
            flush_paragraph()
            result_lines.append(stripped)
        else:
            # Regular paragraph text - collect for joining
            if stripped:
                current_paragraph.append(stripped)

    flush_paragraph()

    # Rejoin with newlines
    text = "\n".join(result_lines)

    # Clean up multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Restore tables
    for i, table in enumerate(tables):
        text = text.replace(f"\x00TABLE{i}\x00", table)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text.strip()


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
    # First flatten text to join single newlines into spaces
    text = flatten_text(text)

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


def split_text_into_blocks(
    text: str, block_type: str = "section", max_length: int = MAX_TEXT_LENGTH
) -> list[dict]:
    """Split long text into multiple Slack blocks to preserve all content.

    Parameters
    ----------
    text : str
        Text to split.
    block_type : str
        Type of block to create ("section" or "context").
    max_length : int
        Maximum length per block.

    Returns
    -------
    list[dict]
        List of Slack blocks containing all the text.
    """
    if len(text) <= max_length:
        if block_type == "context":
            return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    blocks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunk = remaining
            remaining = ""
        else:
            # Find a good break point (newline or space) near the limit
            break_at = max_length
            # Try to break at a newline first
            newline_pos = remaining.rfind("\n", 0, max_length)
            if newline_pos > max_length // 2:
                break_at = newline_pos + 1
            else:
                # Fall back to breaking at a space
                space_pos = remaining.rfind(" ", 0, max_length)
                if space_pos > max_length // 2:
                    break_at = space_pos + 1

            chunk = remaining[:break_at].rstrip()
            remaining = remaining[break_at:].lstrip()

        if chunk:
            if block_type == "context":
                blocks.append(
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": chunk}]}
                )
            else:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    return blocks
