"""Adapts Claude Agent SDK message types to the shared StreamMessage format."""

import json
import time
from typing import Optional

from loguru import logger

from src.backends.tool_summary_registry import build_tool_summary_rules
from src.utils.stream_models import BaseToolActivity, StreamMessage, concat_with_spacing

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


class ToolActivity(BaseToolActivity):
    """Claude-specific tool activity metadata."""

    SUMMARY_RULES = CLAUDE_TOOL_SUMMARY_RULES


def _coerce_text(value: object) -> str:
    """Coerce a value to a string for display."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        return str(value)


class SDKStreamAdapter:
    """Translates Claude Agent SDK messages into StreamMessage objects.

    Maintains accumulated state like the old StreamParser so that the
    final ``result`` message carries the full conversation text.
    """

    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.accumulated_content: str = ""
        self.accumulated_detailed: str = ""
        self.pending_tools: dict[str, BaseToolActivity] = {}

    def adapt(self, sdk_message: object) -> Optional[StreamMessage]:
        """Convert a single SDK message into a StreamMessage (or None to skip)."""
        # Import SDK types here to keep module-level import light and allow
        # the rest of the codebase to load without the SDK installed.
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TaskNotificationMessage,
            TaskStartedMessage,
            UserMessage,
        )
        from claude_agent_sdk.types import TextBlock, ToolResultBlock, ToolUseBlock

        if isinstance(sdk_message, SystemMessage):
            return self._adapt_system(sdk_message)
        if isinstance(sdk_message, AssistantMessage):
            return self._adapt_assistant(sdk_message, TextBlock, ToolUseBlock)
        if isinstance(sdk_message, UserMessage):
            return self._adapt_user(sdk_message, ToolResultBlock)
        if isinstance(sdk_message, ResultMessage):
            return self._adapt_result(sdk_message)
        if isinstance(sdk_message, TaskStartedMessage):
            return self._adapt_task_started(sdk_message)
        if isinstance(sdk_message, TaskNotificationMessage):
            return self._adapt_task_notification(sdk_message)
        if isinstance(sdk_message, RateLimitEvent):
            logger.debug(f"Rate limit event: status={sdk_message.rate_limit_info.status}")
            return None
        if isinstance(sdk_message, StreamEvent):
            # Partial streaming update — not emitted as a StreamMessage
            return None
        logger.debug(f"Unhandled SDK message type: {type(sdk_message).__name__}")
        return None

    # ------------------------------------------------------------------
    # Private adapters
    # ------------------------------------------------------------------

    def _adapt_system(self, msg: object) -> StreamMessage:
        data = msg.data or {}
        self.session_id = data.get("session_id")
        return StreamMessage(
            type="init",
            session_id=self.session_id,
            raw=data,
        )

    def _adapt_assistant(
        self,
        msg: object,
        text_block_cls: type,
        tool_use_block_cls: type,
    ) -> StreamMessage:
        text_content = ""
        detailed_content = ""
        tool_activities: list[ToolActivity] = []

        for block in msg.content:
            if isinstance(block, text_block_cls):
                text = _coerce_text(block.text)
                text_content += text
                detailed_content += text
            elif isinstance(block, tool_use_block_cls):
                tool_id = block.id
                tool_name = block.name
                tool_input = block.input if isinstance(block.input, dict) else {}

                activity = ToolActivity(
                    id=tool_id,
                    name=tool_name,
                    input=tool_input,
                    input_summary=ToolActivity.create_input_summary(tool_name, tool_input),
                    started_at=time.time(),
                )
                self.pending_tools[tool_id] = activity
                tool_activities.append(activity)

                # Build detailed description for tool call
                summary = activity.input_summary
                tool_detailed = f"\n[Tool: {tool_name}] {summary}"
                detailed_content += tool_detailed

        if text_content:
            self.accumulated_content = concat_with_spacing(self.accumulated_content, text_content)
        if detailed_content:
            self.accumulated_detailed += detailed_content

        raw: dict = {}
        if msg.usage:
            raw["usage"] = msg.usage

        return StreamMessage(
            type="assistant",
            content=text_content,
            detailed_content=detailed_content,
            tool_activities=tool_activities,
            session_id=self.session_id,
            raw=raw,
        )

    def _adapt_user(
        self,
        msg: object,
        tool_result_block_cls: type,
    ) -> StreamMessage:
        detailed_addition = ""
        tool_activities: list[ToolActivity] = []

        for block in msg.content:
            if not isinstance(block, tool_result_block_cls):
                continue

            tool_use_id = block.tool_use_id
            is_error = block.is_error
            raw_content = block.content

            # Normalize content to string
            if isinstance(raw_content, str):
                full_content = raw_content
            elif isinstance(raw_content, list):
                parts = []
                for item in raw_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(_coerce_text(item.get("text", "")))
                    elif isinstance(item, str):
                        parts.append(item)
                full_content = "".join(parts)
            else:
                full_content = _coerce_text(raw_content)

            # Resolve pending tool
            pending = self.pending_tools.pop(tool_use_id, None)
            if pending:
                pending.result = full_content[:500] if full_content else ""
                pending.full_result = full_content
                pending.is_error = is_error
                if pending.started_at:
                    pending.duration_ms = int((time.time() - pending.started_at) * 1000)
                tool_activities.append(pending)
                status = "ERROR" if is_error else "OK"
                detailed_addition += f"\n[Result: {pending.name}] ({status})"
            else:
                # Orphaned tool result — create a placeholder activity
                activity = ToolActivity(
                    id=tool_use_id,
                    name="unknown",
                    input={},
                    input_summary="",
                    result=full_content[:500] if full_content else "",
                    full_result=full_content,
                    is_error=is_error,
                )
                tool_activities.append(activity)
                detailed_addition += f"\n[Result: unknown/{tool_use_id[:8]}]"

        if detailed_addition:
            self.accumulated_detailed += detailed_addition

        raw: dict = {}
        if msg.tool_use_result:
            raw["tool_use_result"] = msg.tool_use_result

        return StreamMessage(
            type="user",
            detailed_content=detailed_addition,
            tool_activities=tool_activities,
            session_id=self.session_id,
            raw=raw,
        )

    def _adapt_result(self, msg: object) -> StreamMessage:
        self.pending_tools.clear()

        result_text = _coerce_text(msg.result) if msg.result else ""
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
            session_id=msg.session_id or self.session_id,
            is_final=True,
            cost_usd=msg.total_cost_usd,
            duration_ms=msg.duration_ms,
            raw={
                "is_error": msg.is_error,
                "stop_reason": msg.stop_reason,
                "num_turns": msg.num_turns,
                "usage": msg.usage,
                "subtype": msg.subtype,
            },
        )

    def _adapt_task_started(self, msg: object) -> StreamMessage:
        """Adapt a Task started notification into an assistant-like message."""
        tool_input = {
            "description": msg.description or "",
            "task_id": msg.task_id or "",
        }
        if msg.task_type:
            tool_input["subagent_type"] = msg.task_type

        activity = ToolActivity(
            id=msg.tool_use_id or msg.task_id or "",
            name="Task",
            input=tool_input,
            input_summary=ToolActivity.create_input_summary("Task", tool_input),
            started_at=time.time(),
        )
        tool_id = msg.tool_use_id or msg.task_id or ""
        self.pending_tools[tool_id] = activity

        detailed = f"\n[Tool: Task] {activity.input_summary}"
        self.accumulated_detailed += detailed

        return StreamMessage(
            type="assistant",
            detailed_content=detailed,
            tool_activities=[activity],
            session_id=self.session_id,
            raw={"task_id": msg.task_id, "task_type": msg.task_type},
        )

    def _adapt_task_notification(self, msg: object) -> StreamMessage:
        """Adapt a Task completion notification into a user-like tool-result message."""
        tool_id = msg.tool_use_id or msg.task_id or ""
        pending = self.pending_tools.pop(tool_id, None)

        result_text = msg.summary or ""
        is_error = msg.status in ("error", "failed")

        if pending:
            pending.result = result_text[:500] if result_text else ""
            pending.full_result = result_text
            pending.is_error = is_error
            if pending.started_at:
                pending.duration_ms = int((time.time() - pending.started_at) * 1000)
            activities = [pending]
        else:
            activities = [
                ToolActivity(
                    id=tool_id,
                    name="Task",
                    input={},
                    input_summary="",
                    result=result_text[:500] if result_text else "",
                    full_result=result_text,
                    is_error=is_error,
                )
            ]

        status = "ERROR" if is_error else "OK"
        detailed = f"\n[Result: Task] ({status})"
        self.accumulated_detailed += detailed

        return StreamMessage(
            type="user",
            detailed_content=detailed,
            tool_activities=activities,
            session_id=self.session_id,
            raw={
                "task_id": msg.task_id,
                "status": msg.status,
                "tool_use_id": msg.tool_use_id,
            },
        )
