"""Content rendering with syntax highlighting and diff view."""

from difflib import unified_diff
from typing import Optional

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename


def render_code(content: str, file_path: Optional[str] = None) -> tuple[str, str]:
    """Render code with syntax highlighting.

    Parameters
    ----------
    content : str
        The code content to render.
    file_path : Optional[str]
        File path for language detection.

    Returns
    -------
    tuple[str, str]
        Tuple of (highlighted HTML, CSS styles).
    """
    lexer = _get_lexer(file_path)
    formatter = HtmlFormatter(
        linenos=True,
        cssclass="source",
        style="monokai",
    )

    highlighted = highlight(content, lexer, formatter)
    css = formatter.get_style_defs(".source")

    return highlighted, css


def render_diff(
    old_content: str,
    new_content: str,
    file_path: Optional[str] = None,
) -> tuple[str, str, str]:
    """Render a diff with side-by-side view.

    Parameters
    ----------
    old_content : str
        The original content.
    new_content : str
        The modified content.
    file_path : Optional[str]
        File path for display.

    Returns
    -------
    tuple[str, str, str]
        Tuple of (unified diff text, side-by-side HTML, CSS styles).
    """
    # Generate unified diff
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    from_file = f"a/{file_path}" if file_path else "a/file"
    to_file = f"b/{file_path}" if file_path else "b/file"

    diff_lines = list(unified_diff(old_lines, new_lines, fromfile=from_file, tofile=to_file))
    unified_text = "".join(diff_lines)

    # Generate side-by-side HTML
    side_by_side_html = _generate_side_by_side(old_content, new_content, file_path)

    # CSS for diff styling
    css = _get_diff_css()

    return unified_text, side_by_side_html, css


def _get_lexer(file_path: Optional[str]):
    """Get Pygments lexer for file path.

    Parameters
    ----------
    file_path : Optional[str]
        The file path for language detection.

    Returns
    -------
    Lexer
        The appropriate Pygments lexer.
    """
    if not file_path:
        return TextLexer()

    try:
        return get_lexer_for_filename(file_path)
    except Exception:
        return TextLexer()


def _generate_side_by_side(
    old_content: str,
    new_content: str,
    file_path: Optional[str] = None,
) -> str:
    """Generate side-by-side diff HTML.

    Parameters
    ----------
    old_content : str
        The original content.
    new_content : str
        The modified content.
    file_path : Optional[str]
        File path for syntax highlighting.

    Returns
    -------
    str
        HTML for side-by-side diff view.
    """
    from difflib import SequenceMatcher

    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    matcher = SequenceMatcher(None, old_lines, new_lines)
    rows = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                rows.append(_make_row(i + 1, old_lines[i], j + 1, new_lines[j], "equal"))
        elif tag == "replace":
            # Handle replace - show both old and new lines
            old_range = range(i1, i2)
            new_range = range(j1, j2)
            max_len = max(len(old_range), len(new_range))

            for idx in range(max_len):
                old_idx = i1 + idx if idx < len(old_range) else None
                new_idx = j1 + idx if idx < len(new_range) else None

                old_line = old_lines[old_idx] if old_idx is not None else ""
                new_line = new_lines[new_idx] if new_idx is not None else ""
                old_num = old_idx + 1 if old_idx is not None else ""
                new_num = new_idx + 1 if new_idx is not None else ""

                rows.append(_make_row(old_num, old_line, new_num, new_line, "replace"))
        elif tag == "delete":
            for i in range(i1, i2):
                rows.append(_make_row(i + 1, old_lines[i], "", "", "delete"))
        elif tag == "insert":
            for j in range(j1, j2):
                rows.append(_make_row("", "", j + 1, new_lines[j], "insert"))

    return f'<table class="diff-table">{"".join(rows)}</table>'


def _make_row(
    old_num: int | str,
    old_line: str,
    new_num: int | str,
    new_line: str,
    change_type: str,
) -> str:
    """Make a single row for side-by-side diff.

    Parameters
    ----------
    old_num : int | str
        Old line number or empty.
    old_line : str
        Old line content.
    new_num : int | str
        New line number or empty.
    new_line : str
        New line content.
    change_type : str
        Type of change (equal, replace, delete, insert).

    Returns
    -------
    str
        HTML table row.
    """
    old_class = ""
    new_class = ""

    if change_type == "delete":
        old_class = "diff-delete"
    elif change_type == "insert":
        new_class = "diff-insert"
    elif change_type == "replace":
        old_class = "diff-delete"
        new_class = "diff-insert"

    # Escape HTML
    old_line_escaped = _escape_html(old_line)
    new_line_escaped = _escape_html(new_line)

    return f"""<tr>
        <td class="line-num {old_class}">{old_num}</td>
        <td class="line-content {old_class}"><pre>{old_line_escaped}</pre></td>
        <td class="line-num {new_class}">{new_num}</td>
        <td class="line-content {new_class}"><pre>{new_line_escaped}</pre></td>
    </tr>"""


def _escape_html(text: str) -> str:
    """Escape HTML special characters.

    Parameters
    ----------
    text : str
        Text to escape.

    Returns
    -------
    str
        Escaped text.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _get_diff_css() -> str:
    """Get CSS styles for diff view.

    Returns
    -------
    str
        CSS styles.
    """
    return """
        .diff-table {
            width: 100%;
            border-collapse: collapse;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 13px;
        }
        .diff-table td {
            padding: 0;
            vertical-align: top;
        }
        .diff-table .line-num {
            width: 50px;
            min-width: 50px;
            text-align: right;
            padding: 0 10px;
            color: #6e7681;
            background: #161b22;
            user-select: none;
        }
        .diff-table .line-content {
            width: 50%;
            padding: 0 10px;
            background: #0d1117;
            white-space: pre;
            overflow-x: auto;
        }
        .diff-table .line-content pre {
            margin: 0;
            padding: 2px 0;
            white-space: pre;
        }
        .diff-delete {
            background: #3c1f1e !important;
        }
        .diff-delete .line-num {
            background: #4d2626 !important;
        }
        .diff-insert {
            background: #1e3a1f !important;
        }
        .diff-insert .line-num {
            background: #264d26 !important;
        }
    """
