import json
from typing import Optional

from loguru import logger

from src.backends.stream_parser_base import BaseStreamParser
from src.backends.tool_summary_registry import build_tool_summary_rules
from src.utils.stream_models import (
    BaseToolActivity,
    StreamMessage,
    concat_with_spacing,
)

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
# Increased to 1MB to handle large file reads and tool outputs
MAX_BUFFER_SIZE = 1024 * 1024  # 1MB

CLAUDE_TOOL_SUMMARY_RULES = build_tool_summary_rules(
    {
        "Read": "read",
        "Edit": "edit",
        "Write": "write",
        "Bash": "shell",
        "Glob": "glob",
        "Grep": "grep",
        "Task": "task",
        "WebFetch": "web_fetch",
        "WebSearch": "web_search",
        "LSP": "lsp",
        "TodoWrite": "todo_write",
        "AskUserQuestion": "ask_user",
    }
)

_concat_with_spacing = concat_with_spacing


class ToolActivity(BaseToolActivity):
    """Claude-specific tool activity metadata."""

    SUMMARY_RULES = CLAUDE_TOOL_SUMMARY_RULES


class StreamParser(BaseStreamParser):
    """Parser for Claude CLI stream-json output format."""

    def __init__(self) -> None:
        super().__init__()
        self._handlers = {
            "system": self._parse_system_message,
            "assistant": self._parse_assistant_message,
            "user": self._parse_user_message,
            "result": self._parse_result_message,
            "error": self._parse_error_message,
        }

    @staticmethod
    def _coerce_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except TypeError:
            return str(value)

    def _parse_system_message(self, data: dict) -> StreamMessage:
        self.session_id = data.get("session_id")
        return StreamMessage(
            type="init",
            session_id=self.session_id,
            raw=data,
        )

    def _parse_assistant_message(self, data: dict) -> StreamMessage:
        message = data.get("message", {})
        if isinstance(message, str):
            text_content = message
            self.accumulated_content += text_content
            self.accumulated_detailed += text_content
            return StreamMessage(
                type="assistant",
                content=text_content,
                detailed_content=text_content,
                session_id=self.session_id,
                raw=data,
            )
        if not isinstance(message, dict):
            message = {}
        content_blocks = message.get("content", [])
        if isinstance(content_blocks, str):
            text_content = content_blocks
            self.accumulated_content += text_content
            self.accumulated_detailed += text_content
            return StreamMessage(
                type="assistant",
                content=text_content,
                detailed_content=text_content,
                session_id=self.session_id,
                raw=data,
            )
        if not isinstance(content_blocks, list):
            content_blocks = []

        text_content = ""
        detailed_content = ""
        tool_activities = []

        for block in content_blocks:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    text_content += block
                    detailed_content += block
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                text_content += self._coerce_text(text)
                detailed_content += self._coerce_text(text)
            elif block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})

                tool_activity, tool_detailed, collision = self._create_tool_call_activity(
                    tool_cls=ToolActivity,
                    tool_id=tool_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
                tool_activities.append(tool_activity)
                if collision:
                    logger.warning(
                        f"Tool ID collision detected: {tool_id} already tracked. "
                        "This may indicate duplicate tool invocations."
                    )
                detailed_content += tool_detailed

        if text_content:
            self.accumulated_content = _concat_with_spacing(self.accumulated_content, text_content)
        if detailed_content:
            self.accumulated_detailed += detailed_content

        return StreamMessage(
            type="assistant",
            content=text_content,
            detailed_content=detailed_content,
            tool_activities=tool_activities,
            session_id=self.session_id,
            raw=data,
        )

    def _parse_user_message(self, data: dict) -> StreamMessage:
        message = data.get("message", {})
        if isinstance(message, str):
            detailed_addition = message
            if detailed_addition:
                self.accumulated_detailed += detailed_addition
            return StreamMessage(
                type="user",
                detailed_content=detailed_addition,
                session_id=self.session_id,
                raw=data,
            )
        if not isinstance(message, dict):
            message = {}
        content_blocks = message.get("content", [])
        if isinstance(content_blocks, str):
            detailed_addition = content_blocks
            if detailed_addition:
                self.accumulated_detailed += detailed_addition
            return StreamMessage(
                type="user",
                detailed_content=detailed_addition,
                session_id=self.session_id,
                raw=data,
            )
        if not isinstance(content_blocks, list):
            content_blocks = []

        detailed_addition = ""
        tool_activities = []
        for block in content_blocks:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    detailed_addition += block
                continue
            if block.get("type") != "tool_result":
                continue

            tool_use_id = block.get("tool_use_id", "unknown")
            content = block.get("content", "")
            is_error = block.get("is_error", False)
            if isinstance(content, str):
                full_content = content
            elif isinstance(content, list):
                full_content = ""
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        full_content += self._coerce_text(item.get("text", ""))
                    elif isinstance(item, str):
                        full_content += item
            else:
                full_content = self._coerce_text(content)
            parsed_activities, parsed_detailed = self._create_tool_result_activities(
                tool_cls=ToolActivity,
                tool_use_id=tool_use_id,
                content=full_content,
                is_error=is_error,
            )
            tool_activities.extend(parsed_activities)
            detailed_addition += parsed_detailed

        if detailed_addition:
            self.accumulated_detailed += detailed_addition

        return StreamMessage(
            type="user",
            detailed_content=detailed_addition,
            tool_activities=tool_activities,
            session_id=self.session_id,
            raw=data,
        )

    def _parse_result_message(self, data: dict) -> StreamMessage:
        self.pending_tools.clear()
        result_text = self._coerce_text(data.get("result", ""))
        if not result_text:
            result_text = self._coerce_text(data.get("message", ""))
        final_content = self.accumulated_content
        if result_text:
            if final_content:
                if result_text not in final_content:
                    final_content = f"{final_content}\n\n{result_text}"
            else:
                final_content = result_text

        return StreamMessage(
            type="result",
            content=final_content,
            detailed_content=self.accumulated_detailed,
            session_id=data.get("session_id", self.session_id),
            is_final=True,
            cost_usd=data.get("cost_usd"),
            duration_ms=data.get("duration_ms"),
            raw=data,
        )

    @staticmethod
    def _parse_error_message(data: dict) -> StreamMessage:
        return StreamMessage(
            type="error",
            content=data.get("error", {}).get("message", "Unknown error"),
            is_final=True,
            raw=data,
        )

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of stream-json output."""
        data, overflow_message = self._parse_json_line(
            line,
            max_buffer_size=MAX_BUFFER_SIZE,
        )
        if overflow_message:
            return overflow_message
        if data is None:
            return None

        if not isinstance(data, dict):
            # Handle unexpected non-object JSON (e.g., a JSON string) as plain text output.
            return self._assistant_message_from_non_dict(data)

        msg_type = data.get("type", "unknown")
        handler = self._handlers.get(msg_type)
        if handler:
            return handler(data)
        return StreamMessage(type=msg_type, raw=data)
