"""Shared base parser state and iteration utilities for backend stream parsers."""

from typing import Iterator, Optional

from loguru import logger

from src.backends.stream_parsing_common import (
    create_tool_activity,
    create_tool_result,
    parse_json_line_with_buffer,
)
from src.utils.stream_models import BaseToolActivity, StreamMessage


class BaseStreamParser:
    """Shared parser state and utilities for backend stream parsers."""

    def __init__(self) -> None:
        self.buffer = ""
        self.session_id: Optional[str] = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        # Track pending tool uses to link with results
        self.pending_tools: dict[str, BaseToolActivity] = {}

    def _append_assistant_content(self, content: str) -> None:
        """Append assistant text to accumulated output buffers."""
        if not content:
            return
        self.accumulated_content += content
        self.accumulated_detailed += content

    def _parse_json_line(
        self,
        line: str,
        *,
        max_buffer_size: int,
    ) -> tuple[object | None, StreamMessage | None]:
        """Parse a JSON line and emit overflow errors consistently."""
        data, self.buffer, overflow_error = parse_json_line_with_buffer(
            line=line,
            buffer=self.buffer,
            max_buffer_size=max_buffer_size,
        )
        if overflow_error:
            logger.error(
                f"{overflow_error} This may indicate a malformed JSON stream or extremely large output. Resetting buffer."
            )
            return None, StreamMessage(
                type="error",
                content=(
                    "Stream buffer overflow: "
                    f"JSON chunk exceeded {max_buffer_size // 1024}KB limit"
                ),
                raw={},
            )
        return data, None

    def _assistant_message_from_non_dict(self, payload: object) -> StreamMessage:
        """Convert non-object JSON payload into assistant text stream message."""
        content = str(payload)
        self._append_assistant_content(content)
        return StreamMessage(
            type="assistant",
            content=content,
            detailed_content=content,
            session_id=self.session_id,
            raw={},
        )

    def _create_tool_call_activity(
        self,
        *,
        tool_cls: type[BaseToolActivity],
        tool_id: str,
        tool_name: str,
        tool_input: object,
    ) -> tuple[BaseToolActivity, str, bool]:
        """Create and register a tool activity."""
        return create_tool_activity(
            tool_cls=tool_cls,
            pending_tools=self.pending_tools,
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    def _create_tool_result_activities(
        self,
        *,
        tool_cls: type[BaseToolActivity],
        tool_use_id: str,
        content: str,
        is_error: bool,
    ) -> tuple[list[BaseToolActivity], str]:
        """Resolve tool result content into finalized tool activity records."""
        return create_tool_result(
            tool_cls=tool_cls,
            pending_tools=self.pending_tools,
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
        )

    def parse_stream(self, stream: Iterator[str]) -> Iterator[StreamMessage]:
        """Parse a stream of lines."""
        for line in stream:
            msg = self.parse_line(line)
            if msg:
                yield msg

    def reset(self) -> None:
        """Reset parser state."""
        self.buffer = ""
        self.session_id = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        self.pending_tools.clear()
