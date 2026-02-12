"""Slack messaging helper utilities."""

from typing import Any, Optional

from src.config import config
from slack_sdk.errors import SlackApiError

from src.utils.formatting import SlackFormatter
from src.utils.formatters.base import MAX_TEXT_LENGTH, split_text_into_blocks, text_to_rich_text_blocks
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


def _section_elements_to_mrkdwn(elements: list[dict]) -> str:
    """Convert a list of rich_text inline elements to mrkdwn text."""
    parts = []
    for elem in elements:
        if elem.get("type") != "text":
            continue
        text = elem.get("text", "")
        style = elem.get("style", {})
        if style.get("code"):
            text = f"`{text}`"
        if style.get("bold"):
            text = f"*{text}*"
        if style.get("italic"):
            text = f"_{text}_"
        if style.get("strike"):
            text = f"~{text}~"
        parts.append(text)
    return "".join(parts)


def _rich_text_to_plain_text(rich_text_block: dict) -> str:
    """Convert a rich_text block back to mrkdwn-formatted plain text."""
    text_parts = []
    for element in rich_text_block.get("elements", []):
        elem_type = element.get("type", "")
        if elem_type == "rich_text_section":
            section_text = _section_elements_to_mrkdwn(element.get("elements", []))
            if section_text:
                text_parts.append(section_text)
        elif elem_type == "rich_text_list":
            for i, item in enumerate(element.get("elements", []), 1):
                prefix = f"{i}. " if element.get("style") == "ordered" else "• "
                item_text = _section_elements_to_mrkdwn(item.get("elements", []))
                if item_text:
                    text_parts.append(f"\n{prefix}{item_text}")
        elif elem_type == "rich_text_preformatted":
            code_text = ""
            for sub_elem in element.get("elements", []):
                if sub_elem.get("type") == "text":
                    code_text += sub_elem.get("text", "")
            if code_text:
                text_parts.append(f"\n```\n{code_text}\n```\n")
        elif elem_type == "rich_text_quote":
            quote_text = _section_elements_to_mrkdwn(element.get("elements", []))
            if quote_text:
                text_parts.append(f"\n> {quote_text}")
    return "".join(text_parts)


def _fallback_blocks_for_table_blocks(blocks: list[dict]) -> list[dict]:
    fallback_blocks: list[dict] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "table":
            table_text = _table_block_to_markdown(block)
            if table_text:
                fallback_blocks.extend(split_text_into_blocks(table_text, max_length=MAX_TEXT_LENGTH))
        elif block_type == "rich_text":
            # Convert rich_text to section blocks as fallback
            plain_text = _rich_text_to_plain_text(block)
            if plain_text:
                fallback_blocks.extend(split_text_into_blocks(plain_text, max_length=MAX_TEXT_LENGTH))
        else:
            fallback_blocks.append(block)
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
                text_content = segment["content"]
                if not text_content or not text_content.strip():
                    continue
                # Use rich_text blocks for full-width display
                blocks = text_to_rich_text_blocks(text_content)
                max_content_blocks = config.SLACK_MAX_BLOCKS_PER_MESSAGE - 1  # Reserve 1 for header
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
                error_code = e.response.get("error", "")
                if error_code not in ("invalid_blocks", "msg_blocks_too_long"):
                    raise
                fallback_blocks = header_blocks + _fallback_blocks_for_table_blocks(blocks)
                if not fallback_blocks:
                    raise
                # Split fallback blocks if still too many
                max_blocks = config.SLACK_MAX_BLOCKS_PER_MESSAGE
                if len(fallback_blocks) > max_blocks:
                    for fb_start in range(0, len(fallback_blocks), max_blocks):
                        fb_chunk = fallback_blocks[fb_start : fb_start + max_blocks]
                        fb_kwargs = {"channel": channel_id, "text": title, "blocks": fb_chunk}
                        if thread_ts:
                            fb_kwargs["thread_ts"] = thread_ts
                        result = await client.chat_postMessage(**fb_kwargs)
                else:
                    fallback_kwargs = {
                        "channel": channel_id,
                        "text": title,
                        "blocks": fallback_blocks,
                    }
                    if thread_ts:
                        fallback_kwargs["thread_ts"] = thread_ts
                    result = await client.chat_postMessage(**fallback_kwargs)

        return result

    # For format_as_text, use rich_text blocks for full-width display
    if format_as_text:
        # Title header
        title_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        ]
        # Content as rich_text blocks
        content_blocks = text_to_rich_text_blocks(content)
        all_blocks = title_blocks + content_blocks

        # Split into multiple messages if block count exceeds Slack limit
        max_blocks = config.SLACK_MAX_BLOCKS_PER_MESSAGE
        block_chunks = []
        for start in range(0, len(all_blocks), max_blocks):
            block_chunks.append(all_blocks[start : start + max_blocks])

        result = None
        for chunk in block_chunks:
            kwargs = {
                "channel": channel_id,
                "text": title,
                "blocks": chunk,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            try:
                result = await client.chat_postMessage(**kwargs)
            except SlackApiError as e:
                error_code = e.response.get("error", "")
                if error_code not in ("invalid_blocks", "msg_blocks_too_long"):
                    raise
                formatted_content = markdown_to_slack_mrkdwn(content)
                fallback_blocks = split_text_into_blocks(
                    f"*{title}*\n\n{formatted_content}", max_length=MAX_TEXT_LENGTH
                )
                for fb_start in range(0, len(fallback_blocks), max_blocks):
                    fb_chunk = fallback_blocks[fb_start : fb_start + max_blocks]
                    fb_kwargs = {
                        "channel": channel_id,
                        "text": title,
                        "blocks": fb_chunk,
                    }
                    if thread_ts:
                        fb_kwargs["thread_ts"] = thread_ts
                    result = await client.chat_postMessage(**fb_kwargs)
                return result
        return result

    # For code blocks (format_as_text=False), use the existing logic
    code_block_overhead = 6  # "```...```"
    content_limit = config.SLACK_BLOCK_TEXT_LIMIT - code_block_overhead
    if len(content) <= content_limit:
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

    # For larger code block content, split into multiple messages
    chunks = []
    remaining = content

    while remaining:
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

    result = None
    for i, chunk in enumerate(chunks):
        if i == 0:
            # First message includes title
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
