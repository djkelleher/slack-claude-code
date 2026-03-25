"""Shared stream-message accumulation helpers for backend executors."""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.utils.stream_models import StreamMessage

_GIT_COMMAND_PATTERN = re.compile(r"""(^|[\s;&(|'"])git\s""")


def _build_git_tool_event(tool: Any) -> dict[str, Any] | None:
    """Return structured git tool metadata for a completed tool activity."""
    tool_input = tool.input or {}
    command = tool_input.get("command")
    if isinstance(command, str) and _GIT_COMMAND_PATTERN.search(command):
        return {
            "kind": "shell",
            "tool_id": tool.id,
            "tool_name": tool.name,
            "command": command,
            "result": tool.full_result or tool.result or "",
            "is_error": tool.is_error,
            "duration_ms": tool.duration_ms,
        }

    server = str(tool_input.get("server", "")).strip().lower()
    mcp_tool = str(tool_input.get("tool", "")).strip()
    if server == "git":
        return {
            "kind": "mcp",
            "tool_id": tool.id,
            "tool_name": tool.name,
            "server": server,
            "mcp_tool": mcp_tool,
            "result": tool.full_result or tool.result or "",
            "is_error": tool.is_error,
            "duration_ms": tool.duration_ms,
        }

    return None


@dataclass
class StreamAccumulator:
    """Accumulate normalized stream message state into execution-result fields."""

    join_assistant_chunks: Callable[[str, str], str]
    output: str = ""
    detailed_output: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    git_tool_events: list[dict[str, Any]] = field(default_factory=list)
    _stringify_result_errors: bool = field(default=True, repr=False)

    def apply(self, msg: StreamMessage) -> None:
        """Apply a stream message to tracked execution state."""
        if msg.session_id:
            self.session_id = msg.session_id

        if msg.type == "assistant" and msg.content:
            self.output = self.join_assistant_chunks(self.output, msg.content)

        if msg.type == "result":
            self.cost_usd = msg.cost_usd
            self.duration_ms = msg.duration_ms
            if msg.content and not self.output:
                self.output = msg.content
            if msg.detailed_content:
                self.detailed_output = msg.detailed_content

            raw_errors = []
            if msg.raw and msg.raw.get("is_error"):
                raw_errors = msg.raw.get("errors", [])
            if raw_errors:
                if self._stringify_result_errors:
                    self.error_message = "; ".join(str(err) for err in raw_errors)
                else:
                    self.error_message = "; ".join(raw_errors)

        if msg.type == "error":
            self.error_message = msg.content

        if msg.type == "tool_result" and msg.tool_activities:
            for tool in msg.tool_activities:
                git_event = _build_git_tool_event(tool)
                if git_event:
                    self.git_tool_events.append(git_event)

    def result_fields(
        self,
        *,
        success: bool,
        session_id: Optional[str] = None,
        error: Optional[str] = None,
        was_cancelled: bool = False,
    ) -> dict[str, Any]:
        """Build common execution-result fields from accumulated stream state."""
        resolved_session_id = self.session_id if session_id is None else session_id
        resolved_error = self.error_message if error is None else error
        return {
            "success": success,
            "output": self.output,
            "detailed_output": self.detailed_output,
            "session_id": resolved_session_id,
            "error": resolved_error,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "was_cancelled": was_cancelled,
            "git_tool_events": list(self.git_tool_events),
        }
