"""Unit tests for markdown-to-Slack formatting."""

from src.utils.formatters.markdown import markdown_to_slack_mrkdwn


def test_markdown_to_slack_mrkdwn_formats_headers_lists_and_emphasis() -> None:
    """Markdown headings, bullets, bold, and italic should map to Slack mrkdwn."""
    text = "# Release Plan\n- **Ship** the fix\n- _verify_ rollout"

    formatted = markdown_to_slack_mrkdwn(text)

    assert "*Release Plan*" in formatted
    assert "• *Ship* the fix" in formatted
    assert "• _verify_ rollout" in formatted


def test_markdown_to_slack_mrkdwn_preserves_code_and_dunder_names() -> None:
    """Inline code, code blocks, and dunder names should survive conversion."""
    text = "__init__ uses `value_name`\n\n```python\nprint('__main__')\n```"

    formatted = markdown_to_slack_mrkdwn(text)

    assert "`__init__`" in formatted
    assert "`value_name`" in formatted
    assert "```python\nprint('__main__')\n```" in formatted


def test_markdown_to_slack_mrkdwn_strips_filesystem_markdown_links() -> None:
    """Filesystem markdown links should render as plain path text."""
    text = (
        "[tests/quant/options/test_thetadata_training_data.py]"
        "(/home/dan/dev-repos/quantflows/tests/quant/options/"
        "test_thetadata_training_data.py#L115)"
    )

    formatted = markdown_to_slack_mrkdwn(text)

    assert formatted == "tests/quant/options/test_thetadata_training_data.py"


def test_markdown_to_slack_mrkdwn_returns_empty_input_unchanged() -> None:
    """Empty inputs should pass through unchanged."""
    assert markdown_to_slack_mrkdwn("") == ""
