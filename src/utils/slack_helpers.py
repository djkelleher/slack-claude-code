"""Slack messaging helper utilities."""

from typing import Any, Optional

from src.config import config
from slack_sdk.errors import SlackApiError

from src.utils.formatting import SlackFormatter
from src.utils.formatters.base import MAX_TEXT_LENGTH, split_text_into_blocks
from src.utils.formatters.markdown import markdown_to_slack_mrkdwn
from src.utils.formatters.table import extract_tables_from_text, split_text_by_tables


def _table_block_to_markdown(table_block: dict) -> str:
    rows = table_block.get("rows", [])
    if not rows:
        return ""

    def cell_text(cell: dict) -> str:
        return str(cell.get("text", "")).replace("\n", " ").strip()

    header_cells = [cell_text(cell) for cell in rows[0]]
    if not header_cells:
        return ""
    separator = ["---"] * len(header_cells)
    lines = [
        f"| {' | '.join(header_cells)} |",
        f"| {' | '.join(separator)} |",
    ]
    for row in rows[1:]:
        row_cells = [cell_text(cell) for cell in row]
        lines.append(f"| {' | '.join(row_cells)} |")
    return "\n".join(lines).strip()


def _fallback_blocks_for_table_blocks(blocks: list[dict]) -> list[dict]:
    fallback_blocks: list[dict] = []
    for block in blocks:
        if block.get("type") != "table":
            fallback_blocks.append(block)
            continue
        table_text = _table_block_to_markdown(block)
        if not table_text:
            continue
        fallback_blocks.extend(split_text_into_blocks(table_text, max_length=MAX_TEXT_LENGTH))
    return fallback_blocks


def sanitize_snippet_content(content: str) -> str:
    """Remove control characters that can cause Slack to treat content as binary."""

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

    return "".join(char if is_safe_char(char) else " " for char in content)


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


async def post_text_snippet(
    client: Any,
    channel_id: str,
    content: str,
    title: str,
    thread_ts: Optional[str] = None,
    format_as_text: bool = False,
    render_tables: bool = True,
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
    render_tables : bool, optional
        If True and format_as_text is True, converts Markdown tables into Slack table blocks.
        Defaults to True to render tables wherever they appear.

    Returns
    -------
    dict
        The Slack API response from the last message posted.
    """
    if render_tables and format_as_text:
        text_without_tables, table_blocks = extract_tables_from_text(content)
        segments = split_text_by_tables(text_without_tables)

        messages = []
        for segment in segments:
            if segment["type"] == "text":
                formatted_text = markdown_to_slack_mrkdwn(segment["content"])
                if not formatted_text:
                    continue
                blocks = split_text_into_blocks(formatted_text, max_length=MAX_TEXT_LENGTH)
                max_content_blocks = 49  # Slack limit is 50 blocks per message
                for start in range(0, len(blocks), max_content_blocks):
                    messages.append(blocks[start : start + max_content_blocks])
            else:
                table_block = table_blocks[segment["index"]]
                messages.append([table_block])

        if not messages:
            messages = [[{"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}}]]

        total_messages = len(messages)
        result = None
        for i, blocks in enumerate(messages):
            header_blocks = []
            if i == 0:
                title_text = (
                    f"*{title}* (part {i + 1}/{total_messages})"
                    if total_messages > 1
                    else f"*{title}*"
                )
                header_blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": title_text}}
                )
            else:
                header_blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"_continued ({i + 1}/{total_messages})_",
                            }
                        ],
                    }
                )

            payload_blocks = header_blocks + blocks
            kwargs = {"channel": channel_id, "text": title, "blocks": payload_blocks}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                result = await client.chat_postMessage(**kwargs)
            except SlackApiError as e:
                if e.response.get("error") != "invalid_blocks":
                    raise
                fallback_blocks = header_blocks + _fallback_blocks_for_table_blocks(blocks)
                if not fallback_blocks:
                    raise
                fallback_kwargs = {"channel": channel_id, "text": title, "blocks": fallback_blocks}
                if thread_ts:
                    fallback_kwargs["thread_ts"] = thread_ts
                result = await client.chat_postMessage(**fallback_kwargs)

        return result

    # Convert markdown to Slack mrkdwn if format_as_text is True
    if format_as_text:
        content = markdown_to_slack_mrkdwn(content)

    # Calculate overhead for title that gets combined with content
    # For format_as_text: "*{title}*\n\n" = len(title) + 6 chars
    # For code blocks: "```...```" = 6 chars (title is in separate block)
    if format_as_text:
        title_overhead = len(title) + 6  # "*{title}*\n\n"
    else:
        title_overhead = 0  # Title is in a separate section block

    code_block_overhead = 0 if format_as_text else 6  # "```...```"

    # If content is small enough, post as single message
    content_limit = config.SLACK_BLOCK_TEXT_LIMIT - title_overhead - code_block_overhead
    if len(content) <= content_limit:
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

    # First chunk has title overhead, subsequent chunks don't
    is_first_chunk = True
    while remaining:
        # Account for ``` markers (6 chars) if using code blocks
        # First chunk includes title, subsequent chunks don't
        if format_as_text:
            # format: "*{title}* (part X/Y)\n\n{chunk}" for first, just "{chunk}" for rest
            # Extra overhead for "(part X/Y)" ~15 chars max
            overhead = (len(title) + 6 + 15) if is_first_chunk else 0
        else:
            overhead = 6  # "```...```"
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
        is_first_chunk = False

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
            continued_text = f"_continued ({i+1}/{len(chunks)})_"
            if format_as_text:
                blocks = [
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": continued_text}],
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
                        "elements": [{"type": "mrkdwn", "text": continued_text}],
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
