"""Markdown table to Slack table block converter."""

import re
from typing import Optional


MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 20


def _is_table_separator(line: str) -> bool:
    line = line.strip()
    if "|" not in line:
        return False
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 2:
        return False
    for part in parts:
        if not re.match(r"^:?-{3,}:?$", part):
            return False
    return True


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells: list[str] = []
    buffer: list[str] = []
    i = 0
    in_code = False
    code_delim = 0

    while i < len(line):
        ch = line[i]

        if ch == "\\":
            if i + 1 < len(line):
                next_ch = line[i + 1]
                if next_ch in ("|", "\\", "`"):
                    buffer.append(next_ch)
                    i += 2
                    continue
            buffer.append(ch)
            i += 1
            continue

        if ch == "`":
            run = 1
            j = i + 1
            while j < len(line) and line[j] == "`":
                run += 1
                j += 1
            if not in_code:
                in_code = True
                code_delim = run
            else:
                if run == code_delim:
                    in_code = False
                    code_delim = 0
            buffer.append("`" * run)
            i = j
            continue

        if ch == "|" and not in_code:
            cells.append("".join(buffer).strip())
            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    cells.append("".join(buffer).strip())
    return cells


def _strip_inline_markdown(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"_([^_\n]+)_", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def _make_cell(text: str, base_style: Optional[dict] = None) -> dict:
    plain = _strip_inline_markdown(text)
    # Slack table cells only accept raw_text or rich_text; formatting is not supported.
    return {"type": "raw_text", "text": plain or " "}


def parse_markdown_table(table_text: str) -> Optional[list[dict]]:
    """Parse a markdown table into one or more Slack table blocks.

    Parameters
    ----------
    table_text : str
        The markdown table text (lines containing | characters).

    Returns
    -------
    list[dict] or None
        List of Slack table block dicts, or None if parsing fails.
    """
    lines = [line for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 2:
        return None

    separator_index = None
    for idx, line in enumerate(lines):
        if _is_table_separator(line):
            separator_index = idx
            break

    if separator_index is None or separator_index == 0:
        return None

    header_line = lines[separator_index - 1]
    data_lines = lines[separator_index + 1 :]

    header_cells = _split_row(header_line)
    data_cells: list[list[str]] = []

    for line in data_lines:
        if _is_table_separator(line):
            continue
        cells = _split_row(line)
        if cells:
            data_cells.append(cells)

    max_cols = max(len(header_cells), max((len(row) for row in data_cells), default=0))
    if max_cols == 0:
        return None

    if len(header_cells) < max_cols:
        header_cells.extend([""] * (max_cols - len(header_cells)))

    normalized_data = []
    for row in data_cells:
        if len(row) < max_cols:
            row = row + [""] * (max_cols - len(row))
        normalized_data.append(row)

    col_slices = [
        (start, min(start + MAX_TABLE_COLUMNS, max_cols))
        for start in range(0, max_cols, MAX_TABLE_COLUMNS)
    ]
    rows_per_chunk = max(1, MAX_TABLE_ROWS - 1)

    table_blocks: list[dict] = []

    for col_start, col_end in col_slices:
        sliced_header = header_cells[col_start:col_end]
        for row_start in range(0, len(normalized_data), rows_per_chunk):
            chunk_rows = normalized_data[row_start : row_start + rows_per_chunk]
            rows: list[list[dict]] = []
            header_style = {"bold": True}
            rows.append([_make_cell(cell, header_style) for cell in sliced_header])
            for row in chunk_rows:
                sliced = row[col_start:col_end]
                rows.append([_make_cell(cell) for cell in sliced])

            table_blocks.append(
                {
                    "type": "table",
                    "rows": rows,
                }
            )

    return table_blocks


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

    table_blocks = []
    lines = text.split("\n")
    output_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            table_text = "\n".join(table_lines)
            table_chunk_blocks = parse_markdown_table(table_text)
            if table_chunk_blocks:
                for block in table_chunk_blocks:
                    table_blocks.append(block)
                    output_lines.append(f"\x00TABLEBLOCK{len(table_blocks) - 1}\x00")
            else:
                output_lines.extend(table_lines)
            continue

        output_lines.append(line)
        i += 1

    text = "\n".join(output_lines)

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
