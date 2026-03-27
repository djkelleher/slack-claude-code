"""Unit tests for streaming formatter blocks."""

from src.config import config
from src.utils.formatters.streaming import processing_message, streaming_update


def test_streaming_update_separates_status_and_prompt_for_complete() -> None:
    """Completed updates should render status and prompt in separate lines."""
    blocks = streaming_update(
        prompt="Processing queue item 9: append the text fish to file /tmp/t.txt",
        current_output="Done",
        is_complete=True,
    )

    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == ":heavy_check_mark: Complete"
    assert blocks[1]["type"] == "section"
    assert blocks[1]["text"]["text"].startswith("> Processing queue item 9:")
    assert "Complete Processing queue item" not in blocks[0]["text"]["text"]


def test_streaming_update_separates_status_and_prompt_while_streaming() -> None:
    """In-progress updates should keep status and prompt distinct."""
    blocks = streaming_update(
        prompt="Processing queue item 1: run unit tests",
        current_output="partial output",
    )

    assert blocks[0]["text"]["text"] == ":arrows_counterclockwise: Streaming..."
    assert blocks[1]["text"]["text"] == "> Processing queue item 1: run unit tests"


def test_streaming_update_can_preserve_full_output() -> None:
    """Formatter should keep earlier output when truncation is disabled."""
    long_output = ("A" * config.SLACK_BLOCK_TEXT_LIMIT) + "\n" + ("B" * 200)

    blocks = streaming_update(
        prompt="Processing queue item 2: dump output",
        current_output=long_output,
        truncate_output=False,
    )

    rich_text_contents = [
        element["text"]
        for block in blocks
        if block["type"] == "rich_text"
        for element_group in block["elements"]
        for element in element_group.get("elements", [])
        if element.get("type") == "text"
    ]
    rendered_output = "".join(rich_text_contents)

    assert rendered_output.replace("\n", " ") == long_output.replace("\n", " ")
    assert "_... (earlier output truncated)_" not in rendered_output


def test_streaming_update_does_not_truncate_long_prompt() -> None:
    """Streaming status should show the full normalized prompt."""
    prompt = (
        "Processing queue item 13: how can we improve the logic, algorithmic edge, "
        "mathematical edge in /home/dan/dev-repos/code-sigmas/src/handlers/claude/queue.py"
    )

    blocks = streaming_update(
        prompt=prompt,
        current_output="partial output",
    )

    assert blocks[1]["text"]["text"] == f"> {prompt}"


def test_processing_message_truncates_prompt_to_slack_limit() -> None:
    """Processing preview should stay under Slack's block text limit."""
    prompt = "run " + ("<edge-case> & " * 600)

    blocks = processing_message(prompt)

    text = blocks[0]["text"]["text"]
    assert len(text) <= config.SLACK_BLOCK_TEXT_LIMIT
    assert text.endswith("...")


def test_streaming_update_truncates_extreme_prompt_to_slack_limit() -> None:
    """Streaming prompt block should stay under Slack's block text limit."""
    prompt = "analyze " + ("<very-long> & " * 700)

    blocks = streaming_update(
        prompt=prompt,
        current_output="partial output",
    )

    prompt_text = blocks[1]["text"]["text"]
    assert len(prompt_text) <= config.SLACK_BLOCK_TEXT_LIMIT
    assert prompt_text.endswith("...")
