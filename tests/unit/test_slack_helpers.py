"""Unit tests for Slack messaging helper utilities."""

from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from src.utils import slack_helpers


def test_table_block_to_markdown_handles_empty_and_rows() -> None:
    assert slack_helpers._table_block_to_markdown({"rows": []}) == ""

    block = {
        "rows": [
            [{"text": "H1"}, {"text": "H2"}],
            [{"text": "a"}, {"text": "b\nline"}],
        ]
    }
    markdown = slack_helpers._table_block_to_markdown(block)
    assert markdown == "| H1 | H2 |\n| --- | --- |\n| a | b line |"


def test_section_elements_to_mrkdwn_formats_styles_and_ignores_non_text() -> None:
    elements = [
        {"type": "text", "text": "plain"},
        {"type": "emoji", "name": "wave"},
        {"type": "text", "text": "x", "style": {"code": True}},
        {"type": "text", "text": "y", "style": {"bold": True}},
        {"type": "text", "text": "z", "style": {"italic": True}},
        {"type": "text", "text": "q", "style": {"strike": True}},
        {
            "type": "text",
            "text": "all",
            "style": {"code": True, "bold": True, "italic": True, "strike": True},
        },
    ]

    result = slack_helpers._section_elements_to_mrkdwn(elements)
    assert result == "plain`x`*y*_z_~q~~_*`all`*_~"


def test_rich_text_to_plain_text_supports_sections_lists_code_and_quotes() -> None:
    block = {
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "text", "text": "Heading", "style": {"bold": True}},
                ],
            },
            {
                "type": "rich_text_list",
                "style": "ordered",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": "First"}]},
                ],
            },
            {
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 1,
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": "Sub"}]},
                ],
            },
            {
                "type": "rich_text_preformatted",
                "elements": [{"type": "text", "text": "print('x')"}],
            },
            {
                "type": "rich_text_quote",
                "elements": [{"type": "text", "text": "quoted"}],
            },
        ]
    }

    text = slack_helpers._rich_text_to_plain_text(block)
    assert "*Heading*" in text
    assert "\n1. First" in text
    assert "\n    • Sub" in text
    assert "```\nprint('x')\n```" in text
    assert "\n> quoted" in text


def test_fallback_blocks_for_table_blocks_converts_supported_block_types() -> None:
    blocks = [
        {
            "type": "table",
            "rows": [[{"text": "H"}], [{"text": "v"}]],
        },
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": "hello"}],
                }
            ],
        },
        {"type": "divider"},
    ]

    fallback = slack_helpers._fallback_blocks_for_table_blocks(blocks)
    assert fallback[0]["type"] == "section"
    assert "| H |" in fallback[0]["text"]["text"]
    assert fallback[1]["type"] == "section"
    assert "hello" in fallback[1]["text"]["text"]
    assert fallback[2] == {"type": "divider"}


def test_sanitize_snippet_content_replaces_control_bytes() -> None:
    content = "ok\x00\x01\x7f\x85still\nline\tend"
    assert slack_helpers.sanitize_snippet_content(content) == "ok    still\nline\tend"


@pytest.mark.asyncio
async def test_post_text_snippet_with_tables_success_multi_message(monkeypatch) -> None:
    monkeypatch.setattr(
        slack_helpers,
        "extract_tables_from_text",
        lambda _content: (
            "ignored",
            [{"type": "table", "rows": [[{"type": "raw_text", "text": "H"}]]}],
        ),
    )
    monkeypatch.setattr(
        slack_helpers,
        "split_text_by_tables",
        lambda _text: [
            {"type": "text", "content": "alpha"},
            {"type": "table", "index": 0},
            {"type": "text", "content": "beta"},
        ],
    )
    monkeypatch.setattr(
        slack_helpers,
        "text_to_rich_text_blocks",
        lambda text: [{"type": "rich_text", "elements": [{"type": "text", "text": text}]}],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1"}, {"ts": "2"}, {"ts": "3"}])

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=True,
    )

    assert result == {"ts": "3"}
    assert client.chat_postMessage.await_count == 3

    first_blocks = client.chat_postMessage.await_args_list[0].kwargs["blocks"]
    assert first_blocks[0]["type"] == "section"
    assert first_blocks[0]["text"]["text"] == "*Title* (part 1/3)"

    second_blocks = client.chat_postMessage.await_args_list[1].kwargs["blocks"]
    assert second_blocks[0]["type"] == "context"
    assert "continued (2/3)" in second_blocks[0]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_post_text_snippet_with_tables_no_messages_uses_placeholder(monkeypatch) -> None:
    monkeypatch.setattr(slack_helpers, "extract_tables_from_text", lambda _content: ("ignored", []))
    monkeypatch.setattr(
        slack_helpers,
        "split_text_by_tables",
        lambda _text: [{"type": "text", "content": "   "}],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "1"})

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=True,
    )

    assert result == {"ts": "1"}
    blocks = client.chat_postMessage.await_args.kwargs["blocks"]
    assert blocks[1]["text"]["text"] == "_No output_"


@pytest.mark.asyncio
async def test_post_text_snippet_with_tables_invalid_blocks_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        slack_helpers,
        "extract_tables_from_text",
        lambda _content: ("ignored", [{"type": "table", "rows": []}]),
    )
    monkeypatch.setattr(
        slack_helpers,
        "split_text_by_tables",
        lambda _text: [{"type": "table", "index": 0}],
    )
    monkeypatch.setattr(
        slack_helpers,
        "_fallback_blocks_for_table_blocks",
        lambda _blocks: [{"type": "section", "text": {"type": "mrkdwn", "text": "fallback"}}],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(
        side_effect=[
            SlackApiError("bad blocks", {"error": "invalid_blocks"}),
            {"ts": "2"},
        ]
    )

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=True,
        thread_ts="123.4",
    )

    assert result == {"ts": "2"}
    assert client.chat_postMessage.await_count == 2
    fallback_kwargs = client.chat_postMessage.await_args_list[1].kwargs
    assert fallback_kwargs["thread_ts"] == "123.4"
    assert fallback_kwargs["blocks"][1]["text"]["text"] == "fallback"


@pytest.mark.asyncio
async def test_post_text_snippet_with_tables_fallback_splits_when_too_many_blocks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(slack_helpers.config, "SLACK_MAX_BLOCKS_PER_MESSAGE", 2, raising=False)
    monkeypatch.setattr(
        slack_helpers,
        "extract_tables_from_text",
        lambda _content: ("ignored", [{"type": "table", "rows": []}]),
    )
    monkeypatch.setattr(
        slack_helpers,
        "split_text_by_tables",
        lambda _text: [{"type": "table", "index": 0}],
    )
    monkeypatch.setattr(
        slack_helpers,
        "_fallback_blocks_for_table_blocks",
        lambda _blocks: [
            {"type": "section", "text": {"type": "mrkdwn", "text": "b1"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "b2"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "b3"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "b4"}},
        ],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(
        side_effect=[
            SlackApiError("too long", {"error": "msg_blocks_too_long"}),
            {"ts": "2"},
            {"ts": "3"},
            {"ts": "4"},
        ]
    )

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=True,
    )

    assert result == {"ts": "4"}
    assert client.chat_postMessage.await_count == 4


@pytest.mark.asyncio
async def test_post_text_snippet_with_tables_nonrecoverable_error_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        slack_helpers,
        "extract_tables_from_text",
        lambda _content: ("ignored", [{"type": "table", "rows": []}]),
    )
    monkeypatch.setattr(
        slack_helpers,
        "split_text_by_tables",
        lambda _text: [{"type": "table", "index": 0}],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(
        side_effect=SlackApiError("bad", {"error": "channel_not_found"})
    )

    with pytest.raises(SlackApiError):
        await slack_helpers.post_text_snippet(
            client=client,
            channel_id="C1",
            content="content",
            title="Title",
            format_as_text=True,
            render_tables=True,
        )


@pytest.mark.asyncio
async def test_post_text_snippet_format_text_chunks_by_block_limit(monkeypatch) -> None:
    monkeypatch.setattr(slack_helpers.config, "SLACK_MAX_BLOCKS_PER_MESSAGE", 2, raising=False)
    monkeypatch.setattr(
        slack_helpers,
        "text_to_rich_text_blocks",
        lambda _content: [
            {"type": "rich_text", "elements": []},
            {"type": "rich_text", "elements": []},
            {"type": "rich_text", "elements": []},
        ],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1"}, {"ts": "2"}])

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=False,
    )

    assert result == {"ts": "2"}
    assert client.chat_postMessage.await_count == 2
    first_chunk = client.chat_postMessage.await_args_list[0].kwargs["blocks"]
    assert first_chunk[0]["type"] == "section"
    assert first_chunk[0]["text"]["text"] == "*Title*"


@pytest.mark.asyncio
async def test_post_text_snippet_format_text_invalid_blocks_uses_mrkdwn_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(slack_helpers.config, "SLACK_MAX_BLOCKS_PER_MESSAGE", 2, raising=False)
    monkeypatch.setattr(
        slack_helpers,
        "text_to_rich_text_blocks",
        lambda _content: [{"type": "rich_text", "elements": []}],
    )
    monkeypatch.setattr(slack_helpers, "markdown_to_slack_mrkdwn", lambda _content: "formatted")
    monkeypatch.setattr(
        slack_helpers,
        "split_text_into_blocks",
        lambda _text, max_length: [
            {"type": "section", "text": {"type": "mrkdwn", "text": "f1"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "f2"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "f3"}},
        ],
    )

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(
        side_effect=[
            SlackApiError("bad blocks", {"error": "invalid_blocks"}),
            {"ts": "2"},
            {"ts": "3"},
        ]
    )

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="content",
        title="Title",
        format_as_text=True,
        render_tables=False,
    )

    assert result == {"ts": "3"}
    assert client.chat_postMessage.await_count == 3


@pytest.mark.asyncio
async def test_post_text_snippet_code_block_short_content(monkeypatch) -> None:
    monkeypatch.setattr(slack_helpers.config, "SLACK_BLOCK_TEXT_LIMIT", 50, raising=False)

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "1"})

    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content="abc",
        title="Title",
        format_as_text=False,
        thread_ts="123.4",
    )

    assert result == {"ts": "1"}
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["thread_ts"] == "123.4"
    assert kwargs["blocks"][1]["text"]["text"] == "```abc```"


@pytest.mark.asyncio
async def test_post_text_snippet_code_block_long_content_splits(monkeypatch) -> None:
    monkeypatch.setattr(slack_helpers.config, "SLACK_BLOCK_TEXT_LIMIT", 20, raising=False)

    client = AsyncMock()
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "1"}, {"ts": "2"}])

    content = "line1\nline2\nline3\nline4"
    result = await slack_helpers.post_text_snippet(
        client=client,
        channel_id="C1",
        content=content,
        title="Title",
        format_as_text=False,
    )

    assert result == {"ts": "2"}
    assert client.chat_postMessage.await_count == 2
    first = client.chat_postMessage.await_args_list[0].kwargs
    second = client.chat_postMessage.await_args_list[1].kwargs
    assert first["blocks"][0]["text"]["text"].startswith("*Title* (part 1/")
    assert second["blocks"][0]["type"] == "context"
    assert "continued" in second["blocks"][0]["elements"][0]["text"]
