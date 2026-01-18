import json
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from loguru import logger

from src.config import config

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
# Increased to 1MB to handle large file reads and tool outputs
MAX_BUFFER_SIZE = 1024 * 1024  # 1MB


@dataclass
class ToolActivity:
    """Structured representation of a tool invocation.

    Tracks the full lifecycle of a tool use from invocation to result.
    """

    id: str  # tool_use_id from Claude
    name: str  # Read, Edit, Write, Bash, Glob, Grep, etc.
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
        if name == "Read":
            path = input_dict.get("file_path", "?")
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "Edit":
            path = input_dict.get("file_path", "?")
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "Write":
            path = input_dict.get("file_path", "?")
            return f"`{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "Bash":
            cmd = input_dict.get("command", "?")
            return f"`{cls._truncate_cmd(cmd, display.truncate_cmd_length)}`"
        elif name == "Glob":
            pattern = input_dict.get("pattern", "?")
            max_len = display.truncate_pattern_length
            return f"`{pattern[:max_len]}{'...' if len(pattern) > max_len else ''}`"
        elif name == "Grep":
            pattern = input_dict.get("pattern", "?")
            max_len = display.truncate_pattern_length
            return f"`{pattern[:max_len]}{'...' if len(pattern) > max_len else ''}`"
        elif name == "Task":
            desc = input_dict.get("description", input_dict.get("prompt", "?"))
            max_len = display.truncate_text_length
            return f"`{desc[:max_len]}{'...' if len(str(desc)) > max_len else ''}`"
        elif name == "WebFetch":
            url = input_dict.get("url", "?")
            max_len = display.truncate_url_length
            return f"`{url[:max_len]}{'...' if len(url) > max_len else ''}`"
        elif name == "WebSearch":
            query = input_dict.get("query", "?")
            max_len = display.truncate_text_length
            return f"`{query[:max_len]}{'...' if len(query) > max_len else ''}`"
        elif name == "LSP":
            op = input_dict.get("operation", "?")
            path = input_dict.get("filePath", "?")
            return f"`{op}` on `{cls._truncate_path(path, display.truncate_path_length)}`"
        elif name == "TodoWrite":
            todos = input_dict.get("todos", [])
            return f"`{len(todos)} items`"
        elif name == "AskUserQuestion":
            questions = input_dict.get("questions", [])
            if questions:
                first_q = questions[0].get("question", "?")
                max_len = display.truncate_text_length
                return f"`{first_q[:max_len]}{'...' if len(first_q) > max_len else ''}`"
            return ""
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
    """Parsed message from Claude's stream-json output."""

    type: str  # init, assistant, user, result, error
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
    """Parser for Claude CLI stream-json output format."""

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
                # Create error message to inform user
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
            detailed_content = ""
            tool_activities = []

            for block in content_blocks:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    text_content += text
                    detailed_content += text
                elif block.get("type") == "tool_use":
                    # Create structured tool activity
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})

                    # Create ToolActivity object
                    tool_activity = ToolActivity(
                        id=tool_id,
                        name=tool_name,
                        input=tool_input,
                        input_summary=ToolActivity.create_input_summary(tool_name, tool_input),
                        started_at=time.monotonic(),  # Use monotonic time for duration
                        timestamp=time.time(),  # Wall-clock time for display
                    )
                    tool_activities.append(tool_activity)

                    # Track for linking with results (detect collisions)
                    if tool_id in self.pending_tools:
                        logger.warning(
                            f"Tool ID collision detected: {tool_id} already tracked. "
                            "This may indicate duplicate tool invocations."
                        )
                    self.pending_tools[tool_id] = tool_activity

                    # Include tool use in detailed output
                    detailed_content += f"\n\n[Tool: {tool_name}]\n"
                    # Format tool input nicely
                    for key, value in tool_input.items():
                        if isinstance(value, str) and len(value) > 100:
                            value_preview = value[:100] + "..."
                        else:
                            value_preview = value
                        detailed_content += f"  {key}: {value_preview}\n"

            if text_content:
                self.accumulated_content += text_content
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

        elif msg_type == "user":
            # User message (tool results)
            message = data.get("message", {})
            content_blocks = message.get("content", [])

            detailed_addition = ""
            tool_activities = []

            for block in content_blocks:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "unknown")
                    content = block.get("content", "")
                    is_error = block.get("is_error", False)

                    # Get full content as string
                    if isinstance(content, str):
                        full_content = content
                    elif isinstance(content, list):
                        # Handle array of content blocks
                        full_content = ""
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                full_content += item.get("text", "")
                            elif isinstance(item, str):
                                full_content += item
                    else:
                        full_content = str(content)

                    content_preview = (
                        full_content[:500] + "..." if len(full_content) > 500 else full_content
                    )

                    # Update linked ToolActivity if we have it
                    if tool_use_id in self.pending_tools:
                        tool_activity = self.pending_tools[tool_use_id]
                        tool_activity.result = content_preview
                        tool_activity.full_result = full_content
                        tool_activity.is_error = is_error
                        # Compute duration using monotonic time
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
                    detailed_addition += f"\n\n[Tool Result: {status}]\n{content_preview}\n"

            if detailed_addition:
                self.accumulated_detailed += detailed_addition

            return StreamMessage(
                type="user",
                detailed_content=detailed_addition,
                tool_activities=tool_activities,
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "result":
            # Final result message
            # Clear pending tools to prevent memory accumulation across sessions
            self.pending_tools.clear()
            return StreamMessage(
                type="result",
                content=self.accumulated_content,
                detailed_content=self.accumulated_detailed,
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
        self.accumulated_detailed = ""
        self.pending_tools.clear()
