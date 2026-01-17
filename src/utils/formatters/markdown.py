"""Markdown to Slack mrkdwn formatter."""

import re


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Convert markdown text to Slack mrkdwn format.

    Handles common markdown patterns:
    - Headers (## Title) → *Title*
    - Bold (**text**) → *text*
    - Italic (*text* or _text_) → _text_
    - Numbered lists (1. Item) → 1. Item
    - Bullet points (- Item or * Item) → • Item
    - Code blocks remain as code blocks
    - Inline code (`code`) remains as `code`

    Parameters
    ----------
    text : str
        Markdown formatted text.

    Returns
    -------
    str
        Slack mrkdwn formatted text.
    """
    if not text:
        return text

    # Preserve code blocks (triple backticks) by replacing them temporarily
    # Use \x00 (null byte) as delimiter since it won't appear in normal text
    # and won't be matched by the bold/italic regexes
    code_blocks = []
    def preserve_code_block(match):
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", preserve_code_block, text)

    # Preserve inline code by replacing temporarily
    inline_codes = []
    def preserve_inline_code(match):
        inline_codes.append(match.group(0))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`\n]+?`", preserve_inline_code, text)

    # Preserve Slack bold by using placeholders for converted bold text
    slack_bolds = []
    def preserve_slack_bold(match):
        slack_bolds.append(f"*{match.group(1)}*")
        return f"\x00SLACKBOLD{len(slack_bolds) - 1}\x00"

    # Convert headers (## Title or ### Title) to bold placeholder
    text = re.sub(r"^#{1,6}\s+(.+?)$", preserve_slack_bold, text, flags=re.MULTILINE)

    # Convert bold markdown (**text** or __text__) to bold placeholder
    text = re.sub(r"__([^_\n]+?)__", preserve_slack_bold, text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", preserve_slack_bold, text)

    # Now convert italic markdown to Slack italic (no conflict with bold now)
    # Handle *text* - single asterisks for italic
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"_\1_", text)
    # Handle _text_ for italic (single underscores)
    text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", r"_\1_", text)

    # Convert bullet points (- Item or * Item) to •
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # Numbered lists remain the same (1. Item)

    # Restore Slack bold
    for i, bold in enumerate(slack_bolds):
        text = text.replace(f"\x00SLACKBOLD{i}\x00", bold)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{i}\x00", code)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text
