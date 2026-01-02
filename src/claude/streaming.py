import json
import logging
from dataclasses import dataclass
from typing import Optional, Iterator

logger = logging.getLogger(__name__)

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
MAX_BUFFER_SIZE = 10000


@dataclass
class StreamMessage:
    """Parsed message from Claude's stream-json output."""

    type: str  # init, assistant, result, error
    content: str = ""
    session_id: Optional[str] = None
    is_final: bool = False
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}


class StreamParser:
    """Parser for Claude CLI stream-json output format."""

    def __init__(self):
        self.buffer = ""
        self.session_id: Optional[str] = None
        self.accumulated_content = ""

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of stream-json output."""
        line = line.strip()
        if not line:
            return None

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Might be partial JSON, buffer it
            self.buffer += line
            # Prevent unbounded buffer growth
            if len(self.buffer) > MAX_BUFFER_SIZE:
                logger.warning("Stream buffer overflow, resetting")
                self.buffer = ""
                return None
            try:
                data = json.loads(self.buffer)
                self.buffer = ""
            except json.JSONDecodeError:
                return None

        msg_type = data.get("type", "unknown")

        if msg_type == "system":
            # System init message contains session_id
            self.session_id = data.get("session_id")
            return StreamMessage(
                type="init",
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "assistant":
            # Assistant message with content
            message = data.get("message", {})
            content_blocks = message.get("content", [])

            text_content = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    text_content += block.get("text", "")

            if text_content:
                self.accumulated_content += text_content

            return StreamMessage(
                type="assistant",
                content=text_content,
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "result":
            # Final result message
            return StreamMessage(
                type="result",
                content=self.accumulated_content,
                session_id=data.get("session_id", self.session_id),
                is_final=True,
                cost_usd=data.get("cost_usd"),
                duration_ms=data.get("duration_ms"),
                raw=data,
            )

        elif msg_type == "error":
            return StreamMessage(
                type="error",
                content=data.get("error", {}).get("message", "Unknown error"),
                is_final=True,
                raw=data,
            )

        return StreamMessage(type=msg_type, raw=data)

    def parse_stream(self, stream: Iterator[str]) -> Iterator[StreamMessage]:
        """Parse a stream of lines."""
        for line in stream:
            msg = self.parse_line(line)
            if msg:
                yield msg

    def reset(self):
        """Reset parser state."""
        self.buffer = ""
        self.session_id = None
        self.accumulated_content = ""
