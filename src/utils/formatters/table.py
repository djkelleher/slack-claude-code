"""Markdown table to Slack table block converter."""

import re
from typing import Optional


def parse_markdown_table(table_text: str) -> Optional[dict]:
    """Parse a markdown table into a Slack table block.

    Parameters
    ----------
    table_text : str
        The markdown table text (lines containing | characters).

    Returns
    -------
    dict or None
        A Slack table block dict, or None if parsing fails.
    """
    lines = table_text.strip().split("\n")
    if len(lines) < 2:
        return None

    rows = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Skip separator lines (|---|---|)
        if re.match(r"^\|?[\s\-:|]+\|?$", line):
            continue

        # Parse cells from the line
        # Remove leading/trailing pipes and split by |
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]

        cells = [cell.strip() for cell in line.split("|")]

        # Convert cells to Slack table cell format
        row_cells = []
        for cell in cells:
            # Convert bold markdown (**text**) to Slack bold (*text*)
            cell = re.sub(r"\*\*(.+?)\*\*", r"*\1*", cell)
            row_cells.append({"type": "raw_text", "text": cell})

        if row_cells:
            rows.append(row_cells)

    if not rows:
        return None

    # Slack table block format
    return {
        "type": "table",
        "rows": rows,
    }


def extract_tables_from_text(text: str) -> tuple[str, list[dict]]:
    """Extract markdown tables from text and convert to Slack table blocks.

    Parameters
    ----------
    text : str
        The text that may contain markdown tables.

    Returns
    -------
    tuple[str, list[dict]]
        A tuple of (text_without_tables, list_of_table_blocks).
        Text without tables has table locations marked with \x00TABLE{i}\x00
        placeholders.
    """
    # Protect code blocks first - don't extract tables from inside code blocks
    code_blocks = []

    def save_code_block(match: re.Match) -> str:
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", save_code_block, text)

    # Find markdown tables: consecutive lines containing | characters
    # Table pattern: lines that have at least 2 pipe characters
    table_pattern = r"(?:^|\n)((?:[ \t]*\|[^\n]*\|[^\n]*\n?)+)"

    tables = []
    table_blocks = []

    def extract_table(match: re.Match) -> str:
        table_text = match.group(1).strip()
        table_block = parse_markdown_table(table_text)
        if table_block:
            table_blocks.append(table_block)
            tables.append(f"\x00TABLEBLOCK{len(table_blocks) - 1}\x00")
            return f"\n\x00TABLEBLOCK{len(table_blocks) - 1}\x00\n"
        # If parsing failed, keep the original table text
        return match.group(0)

    text = re.sub(table_pattern, extract_table, text)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text, table_blocks


def split_text_by_tables(text: str) -> list[dict]:
    """Split text into segments separated by tables.

    Parameters
    ----------
    text : str
        Text with \x00TABLEBLOCK{i}\x00 placeholders.

    Returns
    -------
    list[dict]
        List of segments, each with 'type' ('text' or 'table') and content.
        For text: {'type': 'text', 'content': str}
        For table: {'type': 'table', 'index': int}
    """
    segments = []
    pattern = r"\x00TABLEBLOCK(\d+)\x00"

    last_end = 0
    for match in re.finditer(pattern, text):
        # Add text before this table
        text_before = text[last_end : match.start()].strip()
        if text_before:
            segments.append({"type": "text", "content": text_before})

        # Add table reference
        segments.append({"type": "table", "index": int(match.group(1))})
        last_end = match.end()

    # Add remaining text after last table
    text_after = text[last_end:].strip()
    if text_after:
        segments.append({"type": "text", "content": text_after})

    return segments
