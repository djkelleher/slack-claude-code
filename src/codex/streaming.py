"""Parser for Codex CLI stream-json output format."""

import json
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from loguru import logger

from src.config import config

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
MAX_BUFFER_SIZE = 1024 * 1024  # 1MB


@dataclass
class ToolActivity:
    """Structured representation of a tool invocation.

    Tracks the full lifecycle of a tool use from invocation to result.
    """

    id: str  # tool_use_id from Codex
    name: str  # Read, Edit, Write, Bash, etc.
    input: dict  # Full tool input parameters
    input_summary: str  # Short summary for inline display
    result: Optional[str] = None  # Result content (truncated for display)
    full_result: Optional[str] = None  # Full untruncated result
    is_error: bool = False
    duration_ms: Optional[int] = None
    started_at: Optional[float] = None  # time.monotonic() for duration calculation
    timestamp: Optional[float] = None  # time.time() wall-clock for display

    @classmethod
    def create_input_summary(cls, name: str, input_dict: dict) -> str:
        """Create a short summary of tool input for inline display."""
        display = config.timeouts.display
        if name == "read_file":
            path = input_dict.get("path", input_dict.get("file_path", "?"))
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "edit_file":
            path = input_dict.get("path", input_dict.get("file_path", "?"))
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "write_file":
            path = input_dict.get("path", input_dict.get("file_path", "?"))
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "shell" or name == "run_command":
            cmd = input_dict.get("command", input_dict.get("cmd", "?"))
            return f"`{cls._truncate_cmd(cmd, display.truncate_cmd_length)}`"
        elif name == "glob" or name == "find_files":
            pattern = input_dict.get("pattern", "?")
            max_len = display.truncate_pattern_length
            return f"`{pattern[:max_len]}{'...' if len(pattern) > max_len else ''}`"
        elif name == "grep" or name == "search":
            pattern = input_dict.get("pattern", input_dict.get("query", "?"))
            max_len = display.truncate_pattern_length
            return f"`{pattern[:max_len]}{'...' if len(str(pattern)) > max_len else ''}`"
        elif name == "web_fetch":
            url = input_dict.get("url", "?")
            max_len = display.truncate_url_length
            return f"`{url[:max_len]}{'...' if len(url) > max_len else ''}`"
        elif name == "web_search":
            query = input_dict.get("query", "?")
            max_len = display.truncate_text_length
            return f"`{query[:max_len]}{'...' if len(query) > max_len else ''}`"
        else:
            # Generic summary
            return ""

    @staticmethod
    def _truncate_path(path: str, max_len: int = 45) -> str:
        """Truncate file path, keeping filename visible."""
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3) :]

    @staticmethod
    def _truncate_cmd(cmd: str, max_len: int = 50) -> str:
        """Truncate command for display."""
        cmd = cmd.replace("\n", " ").strip()
        if len(cmd) <= max_len:
            return cmd
        return cmd[: max_len - 3] + "..."


@dataclass
class StreamMessage:
    """Parsed message from Codex's stream-json output."""

    type: str  # init, assistant, user, result, error, tool_call, tool_result
    content: str = ""
    detailed_content: str = ""  # Full output with tool use details
    tool_activities: Optional[list[ToolActivity]] = None  # Structured tool data
    session_id: Optional[str] = None
    is_final: bool = False
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}
        if self.tool_activities is None:
            self.tool_activities = []


class StreamParser:
    """Parser for Codex CLI stream-json output format.

    Codex uses newline-delimited JSON events when run with --json flag.
    Event types include:
    - session_start: Session initialization
    - message: Assistant messages (text content)
    - tool_call: Tool invocation
    - tool_result: Tool execution result
    - done: Completion event
    - error: Error event
    """

    def __init__(self):
        self.buffer = ""
        self.session_id: Optional[str] = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        # Track pending tool uses to link with results
        self.pending_tools: dict[str, ToolActivity] = {}  # tool_use_id -> ToolActivity

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
                logger.error(
                    f"Stream buffer overflow ({len(self.buffer)} bytes exceeds {MAX_BUFFER_SIZE} limit). "
                    "This may indicate a malformed JSON stream or extremely large output. Resetting buffer."
                )
                self.buffer = ""
                return StreamMessage(
                    type="error",
                    content=f"Stream buffer overflow: JSON chunk exceeded {MAX_BUFFER_SIZE // 1024}KB limit",
                    raw={},
                )
            try:
                data = json.loads(self.buffer)
                self.buffer = ""
            except json.JSONDecodeError:
                return None

        # Determine event type - Codex uses different event structure
        event_type = data.get("type", data.get("event", "unknown"))

        if event_type == "session_start":
            # Session initialization
            self.session_id = data.get("session_id", data.get("id"))
            return StreamMessage(
                type="init",
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "message" or event_type == "assistant":
            # Assistant message with content
            content = data.get("content", data.get("text", ""))
            if isinstance(content, list):
                # Handle array of content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "".join(text_parts)

            if content:
                self.accumulated_content += content
                self.accumulated_detailed += content

            return StreamMessage(
                type="assistant",
                content=content,
                detailed_content=content,
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "tool_call":
            # Tool invocation
            tool_id = data.get("id", data.get("tool_use_id", ""))
            tool_name = data.get("name", data.get("tool", "unknown"))
            tool_input = data.get("input", data.get("arguments", {}))

            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except json.JSONDecodeError:
                    tool_input = {"raw": tool_input}

            # Create ToolActivity object
            tool_activity = ToolActivity(
                id=tool_id,
                name=tool_name,
                input=tool_input,
                input_summary=ToolActivity.create_input_summary(tool_name, tool_input),
                started_at=time.monotonic(),
                timestamp=time.time(),
            )

            # Track for linking with results
            if tool_id in self.pending_tools:
                logger.warning(
                    f"Tool ID collision detected: {tool_id} already tracked. "
                    "This may indicate duplicate tool invocations."
                )
            self.pending_tools[tool_id] = tool_activity

            # Include tool use in detailed output
            detailed_addition = f"\n\n[Tool: {tool_name}]\n"
            for key, value in tool_input.items():
                if isinstance(value, str) and len(value) > 100:
                    value_preview = value[:100] + "..."
                else:
                    value_preview = value
                detailed_addition += f"  {key}: {value_preview}\n"

            self.accumulated_detailed += detailed_addition

            return StreamMessage(
                type="tool_call",
                tool_activities=[tool_activity],
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "tool_result":
            # Tool execution result
            tool_use_id = data.get("tool_use_id", data.get("id", "unknown"))
            content = data.get("content", data.get("output", data.get("result", "")))
            is_error = data.get("is_error", data.get("error", False))

            if isinstance(is_error, str):
                is_error = is_error.lower() == "true"

            # Get full content as string
            if isinstance(content, str):
                full_content = content
            elif isinstance(content, list):
                full_content = ""
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        full_content += item.get("text", "")
                    elif isinstance(item, str):
                        full_content += item
            else:
                full_content = str(content) if content else ""

            content_preview = (
                full_content[:500] + "..." if len(full_content) > 500 else full_content
            )

            tool_activities = []

            # Update linked ToolActivity if we have it
            if tool_use_id in self.pending_tools:
                tool_activity = self.pending_tools[tool_use_id]
                tool_activity.result = content_preview
                tool_activity.full_result = full_content
                tool_activity.is_error = is_error
                if tool_activity.started_at:
                    tool_activity.duration_ms = int(
                        (time.monotonic() - tool_activity.started_at) * 1000
                    )
                tool_activities.append(tool_activity)
            else:
                # Create a result-only activity for untracked tools
                tool_activity = ToolActivity(
                    id=tool_use_id,
                    name="unknown",
                    input={},
                    input_summary="",
                    result=content_preview,
                    full_result=full_content,
                    is_error=is_error,
                )
                tool_activities.append(tool_activity)

            status = "ERROR" if is_error else "SUCCESS"
            detailed_addition = f"\n\n[Tool Result: {status}]\n{content_preview}\n"
            self.accumulated_detailed += detailed_addition

            return StreamMessage(
                type="tool_result",
                detailed_content=detailed_addition,
                tool_activities=tool_activities,
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "done" or event_type == "result":
            # Final result message
            self.pending_tools.clear()
            return StreamMessage(
                type="result",
                content=self.accumulated_content,
                detailed_content=self.accumulated_detailed,
                session_id=data.get("session_id", self.session_id),
                is_final=True,
                cost_usd=data.get("cost_usd", data.get("usage", {}).get("cost")),
                duration_ms=data.get("duration_ms", data.get("duration")),
                raw=data,
            )

        elif event_type == "error":
            error_msg = data.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            return StreamMessage(
                type="error",
                content=str(error_msg),
                is_final=True,
                raw=data,
            )

        # Handle any other message type
        return StreamMessage(type=event_type, raw=data)

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
        self.accumulated_detailed = ""
        self.pending_tools.clear()
