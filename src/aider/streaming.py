"""Stream parser for Aider CLI output.

Aider outputs plain text to stdout with no JSON mode. This parser
handles the text output, filtering out ANSI escape codes and
progress indicators to produce clean StreamMessage objects.
"""

import re
from typing import Optional

from src.backends.stream_parser_base import BaseStreamParser
from src.utils.stream_models import StreamMessage

# Strip ANSI escape sequences (colors, cursor movement, etc.)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07")

# Aider progress/status lines to filter out
_SKIP_PATTERNS = re.compile(
    r"^(Tokens:|Costs:|Model:|Git repo:|Repo-map:|Added|Removed|"
    r"Commit [0-9a-f]+|Applied edit|Creating file|> )"
)


class StreamParser(BaseStreamParser):
    """Parse Aider CLI plain-text output into StreamMessage objects.

    Strips ANSI codes and filters progress lines to produce clean
    assistant content messages.
    """

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of Aider output.

        Parameters
        ----------
        line : str
            A line from Aider's stdout.

        Returns
        -------
        Optional[StreamMessage]
            A StreamMessage for content lines, None for filtered lines.
        """
        stripped = line.rstrip("\n\r")
        if not stripped:
            return None

        # Remove ANSI escape sequences
        clean = _ANSI_ESCAPE.sub("", stripped)
        if not clean.strip():
            return None

        # Filter progress/status lines
        if _SKIP_PATTERNS.match(clean.strip()):
            return None

        self._append_assistant_content(clean + "\n")
        return StreamMessage(
            type="assistant",
            content=clean,
            detailed_content=clean,
            session_id=self.session_id,
            raw={"line": clean},
        )
