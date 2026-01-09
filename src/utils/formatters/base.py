"""Base formatting utilities and constants."""

import re
from datetime import datetime


# Constants
MAX_TEXT_LENGTH = 2900  # Leave room for formatting
FILE_THRESHOLD = 2000  # Attach as file if longer than this


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
    """
    import re
    
    # Protect code blocks and inline code first
    code_blocks = []
    inline_codes = []
    
    # Extract code blocks
    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{len(code_blocks)-1}__"
    
    text = re.sub(r'```[\s\S]*?```', save_code_block, text)
    
    # Extract inline code
    def save_inline_code(match):
        inline_codes.append(match.group(0))
        return f"__INLINE_CODE_{len(inline_codes)-1}__"
    
    text = re.sub(r'`[^`]+`', save_inline_code, text)
    
    # Convert bold: **text** or __text__ -> *text*
    text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', text)
    text = re.sub(r'__([^_]+)__', r'*\1*', text)
    
    # Convert italic: *text* -> _text_ (single asterisk)
    # This is tricky because we just converted bold
    # Look for single asterisks not preceded/followed by another asterisk
    text = re.sub(r'(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)', r'_\1_', text)
    
    # Convert links: [text](url) -> <url|text>
    text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<\2|\1>', text)
    
    # Restore code blocks and inline code
    for i, code in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", code)
    
    for i, code in enumerate(inline_codes):
        text = text.replace(f"__INLINE_CODE_{i}__", code)
    
    # Finally escape special characters
    return escape_markdown(text)


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
        # Find a good break point (newline) near the truncation point
        truncated = output[-max_length + 50:]
        # Try to start at a newline for cleaner truncation
        newline_pos = truncated.find('\n')
        if newline_pos != -1 and newline_pos < 100:
            truncated = truncated[newline_pos + 1:]
        return "_... (earlier output truncated)_\n\n" + truncated
    return output
