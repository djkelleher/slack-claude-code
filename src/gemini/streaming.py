"""Stream parser for Gemini CLI output.

Proof-of-concept parser that handles plain-text output from the Gemini CLI.
When Gemini CLI supports structured JSON output, this can be upgraded to
parse it like Claude's stream-json format.
"""

from typing import Optional

from src.backends.stream_parser_base import BaseStreamParser
from src.utils.stream_models import StreamMessage


class StreamParser(BaseStreamParser):
    """Parse Gemini CLI output into StreamMessage objects.

    Currently handles plain-text output by wrapping lines as assistant
    messages. This is a minimal implementation for the proof-of-concept.
    """

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of Gemini CLI output.

        Parameters
        ----------
        line : str
            A line from the Gemini CLI stdout.

        Returns
        -------
        Optional[StreamMessage]
            A StreamMessage if the line produces output, None otherwise.
        """
        stripped = line.rstrip("\n\r")
        if not stripped:
            return None

        self._append_assistant_content(stripped + "\n")
        return StreamMessage(
            type="assistant",
            content=stripped,
            detailed_content=stripped,
            session_id=self.session_id,
            raw={"line": stripped},
        )
