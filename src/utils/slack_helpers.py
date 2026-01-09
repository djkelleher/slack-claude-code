"""Slack messaging helper utilities."""

from typing import Any, Optional

from src.utils.formatting import SlackFormatter


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
) -> dict:
    """Upload a text file to Slack as a proper text snippet.

    Uses filetype="text" to ensure the file is displayed as text, not binary.

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

    sanitized_content = "".join(
        char if is_safe_char(char) else " "
        for char in content
    )

    kwargs = {
        "channel": channel_id,
        "content": sanitized_content,
        "filename": filename,
        "title": title,
        "filetype": "text",  # Explicitly set filetype to text
        "snippet_type": "text",
    }
    if initial_comment:
        kwargs["initial_comment"] = initial_comment

    return await client.files_upload_v2(**kwargs)
