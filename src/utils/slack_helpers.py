"""Slack messaging helper utilities."""

from typing import Any, Optional

from src.config import config
from src.utils.formatting import SlackFormatter
from src.utils.formatters.markdown import markdown_to_slack_mrkdwn


async def post_error(
    client: Any,
    channel_id: str,
    error_message: str,
    thread_ts: Optional[str] = None,
) -> None:
    """Post a formatted error message to Slack.

    Parameters
    ----------
    client : Any
        Slack WebClient for API calls.
    channel_id : str
        Target channel ID.
    error_message : str
        Error message to display.
    thread_ts : str, optional
        Thread timestamp for replies.
    """
    kwargs = {
        "channel": channel_id,
        "text": f"Error: {error_message}",
        "blocks": SlackFormatter.error_message(error_message),
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    await client.chat_postMessage(**kwargs)


async def update_with_error(
    client: Any,
    channel_id: str,
    message_ts: str,
    error_message: str,
) -> None:
    """Update an existing message to show an error.

    Parameters
    ----------
    client : Any
        Slack WebClient for API calls.
    channel_id : str
        Target channel ID.
    message_ts : str
        Timestamp of message to update.
    error_message : str
        Error message to display.
    """
    await client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=f"Error: {error_message}",
        blocks=SlackFormatter.error_message(error_message),
    )


async def post_success(
    client: Any,
    channel_id: str,
    message: str,
    thread_ts: Optional[str] = None,
) -> dict:
    """Post a simple success message to Slack.

    Parameters
    ----------
    client : Any
        Slack WebClient for API calls.
    channel_id : str
        Target channel ID.
    message : str
        Message to display.
    thread_ts : str, optional
        Thread timestamp for replies.

    Returns
    -------
    dict
        The Slack API response.
    """
    kwargs = {
        "channel": channel_id,
        "text": message,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            }
        ],
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    return await client.chat_postMessage(**kwargs)


async def upload_text_file(
    client: Any,
    channel_id: str,
    content: str,
    filename: str,
    title: str,
    initial_comment: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """Upload a text file to Slack as a proper text snippet.

    Uses filetype="text" to ensure the file is displayed as text, not binary.
    File attachments appear collapsed by default in Slack.

    Parameters
    ----------
    client : Any
        Slack WebClient for API calls.
    channel_id : str
        Target channel ID.
    content : str
        Text content to upload.
    filename : str
        Name for the file.
    title : str
        Display title for the file.
    initial_comment : str, optional
        Comment to post with the file.
    thread_ts : str, optional
        Thread timestamp to post in thread.

    Returns
    -------
    dict
        The Slack API response.
    """

    # Sanitize content to remove control characters that might cause
    # Slack to treat the file as binary (keep printable ASCII + Unicode + whitespace)
    def is_safe_char(char: str) -> bool:
        code = ord(char)
        # Allow: tab, newline, carriage return
        if char in "\n\r\t":
            return True
        # Allow: printable ASCII (space through tilde)
        if 32 <= code <= 126:
            return True
        # Allow: Unicode characters (Latin-1 Supplement and beyond)
        if code >= 160:
            return True
        # Block: null bytes, control chars (0-31 except above), DEL (127), C1 controls (128-159)
        return False

    sanitized_content = "".join(char if is_safe_char(char) else " " for char in content)

    kwargs = {
        "channel": channel_id,
        "content": sanitized_content,
        "filename": filename,
        "title": title,
        "filetype": "text",
        "snippet_type": "text",
    }
    if initial_comment:
        kwargs["initial_comment"] = initial_comment
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    return await client.files_upload_v2(**kwargs)


async def post_text_snippet(
    client: Any,
    channel_id: str,
    content: str,
    title: str,
    thread_ts: Optional[str] = None,
    format_as_text: bool = False,
) -> dict:
    """Post text content as an inline snippet message (no file download needed).

    For large content, splits into multiple messages with code blocks.
    Content appears directly in Slack without requiring a file download.

    Parameters
    ----------
    client : Any
        Slack WebClient for API calls.
    channel_id : str
        Target channel ID.
    content : str
        Text content to post.
    title : str
        Title/header for the snippet.
    thread_ts : str, optional
        Thread timestamp to post in thread.
    format_as_text : bool, optional
        If True, formats markdown as Slack mrkdwn instead of code blocks.
        Converts headers, bold, bullets, etc. Default is False (code block).

    Returns
    -------
    dict
        The Slack API response from the last message posted.
    """
    # Convert markdown to Slack mrkdwn if format_as_text is True
    if format_as_text:
        content = markdown_to_slack_mrkdwn(content)

    # If content is small enough, post as single message
    if len(content) <= config.SLACK_BLOCK_TEXT_LIMIT:
        if format_as_text:
            # Format as text without code block
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{title}*\n\n{content}"},
                },
            ]
        else:
            # Format as code block
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{title}*"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{content}```"},
                },
            ]

        kwargs = {
            "channel": channel_id,
            "text": title,
            "blocks": blocks,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        return await client.chat_postMessage(**kwargs)

    # For larger content, split into multiple messages
    chunks = []
    remaining = content
    while remaining:
        # Account for ``` markers (6 chars) if using code blocks
        overhead = 0 if format_as_text else 6
        chunk_size = config.SLACK_BLOCK_TEXT_LIMIT - overhead
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break

        # Try to break at a newline for cleaner output
        break_point = remaining.rfind("\n", 0, chunk_size)
        if break_point == -1 or break_point < chunk_size // 2:
            break_point = chunk_size

        chunks.append(remaining[:break_point])
        remaining = remaining[break_point:].lstrip("\n")

    result = None
    for i, chunk in enumerate(chunks):
        if i == 0:
            # First message includes title
            if format_as_text:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{title}* (part {i+1}/{len(chunks)})\n\n{chunk}",
                        },
                    },
                ]
            else:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{title}* (part {i+1}/{len(chunks)})",
                        },
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```{chunk}```"},
                    },
                ]
        else:
            if format_as_text:
                blocks = [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"_continued ({i+1}/{len(chunks)})_"}],
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": chunk},
                    },
                ]
            else:
                blocks = [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"_continued ({i+1}/{len(chunks)})_"}],
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```{chunk}```"},
                    },
                ]

        kwargs = {
            "channel": channel_id,
            "text": f"{title} (part {i+1}/{len(chunks)})",
            "blocks": blocks,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        result = await client.chat_postMessage(**kwargs)

    return result
