"""Claude Code executor backed by Claude SDK streaming input mode."""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk.types import CanUseTool, StreamEvent
from loguru import logger

from src.backends.execution_result import BackendExecutionResult
from src.backends.process_executor_base import ProcessExecutorBase
from src.backends.stream_accumulator import StreamAccumulator
from src.utils.stream_models import concat_with_spacing

from ..config import config
from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from ..database.repository import DatabaseRepository

PLAN_WRITE_GRACE_SECONDS = 600.0

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_SDK_PERMISSION_MODE_MAP = {
    "default": "default",
    "acceptedits": "acceptEdits",
    "plan": "plan",
    "bypasspermissions": "bypassPermissions",
    "delegate": "default",
    "dontask": "bypassPermissions",
}

_RISKY_TOOL_NAMES = {
    "bash",
    "edit",
    "multiedit",
    "notebookedit",
    "write",
}

_VALID_TOOL_POLICY_MODES = {
    "off",
    "allow_all",
    "deny_all",
    "denylist",
    "approve_risky",
    "manual",
}


@dataclass
class ExecutionResult(BackendExecutionResult):
    """Result of a Claude SDK execution."""

    has_pending_question: bool = False
    has_pending_plan_approval: bool = False
    plan_subagent_result: Optional[str] = None
    plan_write_timeout: bool = False


@dataclass
class TurnControlResult:
    """Result for steer/interrupt control requests sent to active executions."""

    success: bool
    error: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class _ControlRequest:
    """Queued control request for an active Claude execution."""

    kind: str  # "steer" | "interrupt"
    text: Optional[str]
    future: asyncio.Future[TurnControlResult]


@dataclass
class _ActiveExecution:
    """Live active Claude execution metadata for steer/interrupt routing."""

    session_scope: str
    execution_id: str
    query_session_id: str
    client: ClaudeSDKClient
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    control_queue: asyncio.Queue[_ControlRequest] = field(default_factory=asyncio.Queue)
    session_id: Optional[str] = None
    cancel_requested: bool = False


@dataclass
class ExecutionState:
    """Per-execution state to avoid race conditions between concurrent executions."""

    exit_plan_mode_tool_id: Optional[str] = None
    exit_plan_mode_error_detected: bool = False
    exit_plan_mode_detected: bool = False
    ask_user_question_detected: bool = False
    plan_subagent_tool_id: Optional[str] = None
    plan_subagent_is_plan_type: bool = False
    plan_subagent_completed: bool = False
    plan_subagent_completed_at: Optional[float] = None
    plan_subagent_result: Optional[str] = None
    pending_write_tools: dict[str, str] = field(default_factory=dict)
    exit_plan_mode_detected_at: Optional[float] = None
    plan_write_timeout: bool = False
    plan_write_completed: bool = False
    plan_write_path: Optional[str] = None
    plan_write_wait_logged: bool = False


class SubprocessExecutor(ProcessExecutorBase):
    """Execute Claude Code via Claude SDK streaming-input client."""

    def __init__(
        self,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        super().__init__()
        self.db = db
        self._execution_states: dict[str, ExecutionState] = {}
        self._states_lock: asyncio.Lock = asyncio.Lock()
        self._active_executions_by_scope: dict[str, _ActiveExecution] = {}
        self._active_executions_by_execution_id: dict[str, _ActiveExecution] = {}
        self._active_lock: asyncio.Lock = asyncio.Lock()

    async def _get_current_permission_mode(
        self, db_session_id: Optional[int], fallback_mode: Optional[str]
    ) -> str:
        """Get the current permission mode from the database."""
        if not self.db or not db_session_id:
            return fallback_mode or config.CLAUDE_PERMISSION_MODE

        session = await self.db.get_session_by_id(db_session_id)
        if session and session.permission_mode:
            return session.permission_mode

        return fallback_mode or config.CLAUDE_PERMISSION_MODE

    @staticmethod
    def _normalize_permission_mode(mode: Optional[str]) -> Optional[str]:
        """Normalize app permission mode into SDK-compatible permission mode."""
        if mode is None:
            return None
        mapped = _SDK_PERMISSION_MODE_MAP.get(mode.strip().lower())
        return mapped

    @staticmethod
    def _allowed_tools_list() -> list[str]:
        """Parse configured allowed tools as a list."""
        if not config.ALLOWED_TOOLS:
            return []
        return [
            tool.strip() for tool in config.ALLOWED_TOOLS.split(",") if tool.strip()
        ]

    def _build_sdk_options(
        self,
        *,
        working_directory: str,
        resume_session_id: Optional[str],
        permission_mode: Optional[str],
        model: Optional[str],
        log_prefix: str,
        on_tool_permission_request: Optional[Callable[[str, dict], Awaitable[bool]]],
    ) -> tuple[ClaudeCodeOptions, str]:
        """Build SDK options and initial query session identifier."""
        sdk_mode = self._normalize_permission_mode(
            permission_mode or config.CLAUDE_PERMISSION_MODE
        )
        if sdk_mode is None:
            sdk_mode = self._normalize_permission_mode(config.DEFAULT_BYPASS_MODE)
        if sdk_mode is None:
            sdk_mode = "bypassPermissions"

        initial_query_session_id = "default"
        resume_uuid: Optional[str] = None
        if resume_session_id and UUID_PATTERN.match(resume_session_id):
            resume_uuid = resume_session_id
            initial_query_session_id = resume_session_id
        elif resume_session_id:
            logger.warning(
                f"{log_prefix}Invalid session ID format (not UUID): {resume_session_id}"
            )

        effective_model = model or config.DEFAULT_MODEL
        if effective_model:
            logger.info(f"{log_prefix}Using --model {effective_model}")

        logger.info(f"{log_prefix}Using permission mode {sdk_mode}")
        can_use_tool_callback = self._build_can_use_tool_callback(
            log_prefix=log_prefix,
            on_tool_permission_request=on_tool_permission_request,
        )

        options = ClaudeCodeOptions(
            cwd=working_directory,
            model=effective_model,
            permission_mode=sdk_mode,
            resume=resume_uuid,
            continue_conversation=bool(resume_uuid),
            allowed_tools=self._allowed_tools_list(),
            can_use_tool=can_use_tool_callback,
            include_partial_messages=config.CLAUDE_INCLUDE_PARTIAL_MESSAGES,
        )
        return options, initial_query_session_id

    @staticmethod
    def _normalize_tool_policy_mode(raw_mode: str) -> str:
        """Normalize and validate Claude tool policy mode."""
        normalized = (raw_mode or "").strip().lower()
        if normalized in _VALID_TOOL_POLICY_MODES:
            return normalized
        logger.warning(
            f"Invalid CLAUDE_TOOL_POLICY_MODE='{raw_mode}', falling back to approve_risky"
        )
        return "approve_risky"

    def _build_can_use_tool_callback(
        self,
        *,
        log_prefix: str,
        on_tool_permission_request: Optional[Callable[[str, dict], Awaitable[bool]]],
    ) -> Optional[CanUseTool]:
        """Build SDK tool-permission callback according to configured policy mode."""
        policy_mode = self._normalize_tool_policy_mode(config.CLAUDE_TOOL_POLICY_MODE)
        denylist = set(config.CLAUDE_TOOL_POLICY_DENYLIST)
        if policy_mode == "off":
            logger.info(f"{log_prefix}Claude tool policy disabled (mode=off)")
            return None

        logger.info(f"{log_prefix}Claude tool policy mode: {policy_mode}")

        async def can_use_tool(
            tool_name: str,
            tool_input: dict,
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            del context
            normalized_name = (tool_name or "").strip()
            lowered_name = normalized_name.lower()
            safe_input = tool_input if isinstance(tool_input, dict) else {}

            if policy_mode == "allow_all":
                return PermissionResultAllow()

            if policy_mode == "deny_all":
                return PermissionResultDeny(
                    message=f"{normalized_name} denied by policy"
                )

            if lowered_name in denylist:
                return PermissionResultDeny(
                    message=f"{normalized_name} denied by denylist"
                )

            needs_manual_approval = policy_mode == "manual"
            if policy_mode == "approve_risky":
                needs_manual_approval = lowered_name in _RISKY_TOOL_NAMES

            if not needs_manual_approval:
                return PermissionResultAllow()

            if on_tool_permission_request is None:
                logger.warning(
                    f"{log_prefix}No Slack permission callback for {normalized_name}; allowing"
                )
                return PermissionResultAllow()

            approved = await on_tool_permission_request(normalized_name, safe_input)
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"{normalized_name} denied by user")

        return can_use_tool

    @staticmethod
    def _content_block_to_dict(block: object) -> dict:
        """Convert SDK content blocks to stream-json block dictionaries."""
        if isinstance(block, TextBlock):
            return {"type": "text", "text": block.text}
        if isinstance(block, ThinkingBlock):
            return {
                "type": "thinking",
                "thinking": block.thinking,
                "signature": block.signature,
            }
        if isinstance(block, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if isinstance(block, ToolResultBlock):
            payload = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }
            if block.is_error is not None:
                payload["is_error"] = block.is_error
            return payload
        return {"type": "text", "text": str(block)}

    @staticmethod
    def _extract_partial_text_from_stream_event(event: dict) -> str:
        """Extract text delta payload from a stream event, if present."""
        if not isinstance(event, dict):
            return ""

        delta = event.get("delta")
        if isinstance(delta, dict):
            text = delta.get("text")
            if isinstance(text, str) and text:
                return text

        content_block = event.get("content_block")
        if isinstance(content_block, dict):
            block_text = content_block.get("text")
            if isinstance(block_text, str) and block_text:
                return block_text

        text = event.get("text")
        if isinstance(text, str) and text:
            return text
        return ""

    def _sdk_message_to_raw(self, sdk_message: object) -> Optional[dict]:
        """Convert SDK message objects into Claude stream-json shaped dictionaries."""
        if isinstance(sdk_message, AssistantMessage):
            return {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        self._content_block_to_dict(block)
                        for block in sdk_message.content
                    ],
                    "model": sdk_message.model,
                },
                "parent_tool_use_id": sdk_message.parent_tool_use_id,
            }

        if isinstance(sdk_message, UserMessage):
            if isinstance(sdk_message.content, str):
                content: str | list[dict] = sdk_message.content
            else:
                content = [
                    self._content_block_to_dict(block) for block in sdk_message.content
                ]
            return {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": content,
                },
                "parent_tool_use_id": sdk_message.parent_tool_use_id,
            }

        if isinstance(sdk_message, SystemMessage):
            return sdk_message.data

        if isinstance(sdk_message, ResultMessage):
            errors = (
                [sdk_message.result]
                if sdk_message.is_error and sdk_message.result
                else []
            )
            return {
                "type": "result",
                "subtype": sdk_message.subtype,
                "duration_ms": sdk_message.duration_ms,
                "duration_api_ms": sdk_message.duration_api_ms,
                "is_error": sdk_message.is_error,
                "errors": errors,
                "num_turns": sdk_message.num_turns,
                "session_id": sdk_message.session_id,
                "cost_usd": sdk_message.total_cost_usd,
                "total_cost_usd": sdk_message.total_cost_usd,
                "usage": sdk_message.usage,
                "result": sdk_message.result,
            }

        if isinstance(sdk_message, StreamEvent):
            partial_text = self._extract_partial_text_from_stream_event(
                sdk_message.event
            )
            if partial_text:
                return {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": partial_text}],
                    },
                    "session_id": sdk_message.session_id,
                    "parent_tool_use_id": sdk_message.parent_tool_use_id,
                    "partial": True,
                    "stream_event": sdk_message.event,
                }
            return {
                "type": "stream_event",
                "uuid": sdk_message.uuid,
                "session_id": sdk_message.session_id,
                "event": sdk_message.event,
                "parent_tool_use_id": sdk_message.parent_tool_use_id,
            }

        return None

    async def _register_active_execution(self, active: _ActiveExecution) -> bool:
        """Register active execution for scope and execution-id based control paths."""
        async with self._active_lock:
            existing = self._active_executions_by_scope.get(active.session_scope)
            if existing and not existing.done_event.is_set():
                return False
            self._active_executions_by_scope[active.session_scope] = active
            if active.execution_id:
                self._active_executions_by_execution_id[active.execution_id] = active
            return True

    async def _unregister_active_execution(self, active: _ActiveExecution) -> None:
        """Remove active execution from all tracking maps."""
        async with self._active_lock:
            current = self._active_executions_by_scope.get(active.session_scope)
            if current is active:
                self._active_executions_by_scope.pop(active.session_scope, None)
            if active.execution_id:
                current_by_id = self._active_executions_by_execution_id.get(
                    active.execution_id
                )
                if current_by_id is active:
                    self._active_executions_by_execution_id.pop(
                        active.execution_id, None
                    )

    async def has_active_execution(self, session_scope: str) -> bool:
        """Return True when an execution is currently active for the session scope."""
        async with self._active_lock:
            active = self._active_executions_by_scope.get(session_scope)
            return bool(active and not active.done_event.is_set())

    async def _enqueue_control(
        self,
        session_scope: str,
        *,
        kind: str,
        text: Optional[str] = None,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Queue a steer/interrupt request for the active execution in a scope."""
        async with self._active_lock:
            active = self._active_executions_by_scope.get(session_scope)
            if not active or active.done_event.is_set():
                return TurnControlResult(success=False, error="No active execution")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[TurnControlResult] = loop.create_future()
            request = _ControlRequest(kind=kind, text=text, future=future)
            await active.control_queue.put(request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return TurnControlResult(success=False, error=f"{kind} request timed out")

    async def steer_active_execution(
        self,
        session_scope: str,
        text: str,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Interrupt current Claude turn and stream follow-up input into the same session."""
        return await self._enqueue_control(
            session_scope, kind="steer", text=text, timeout=timeout
        )

    async def interrupt_active_execution(
        self,
        session_scope: str,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Interrupt the active Claude execution in this session scope."""
        return await self._enqueue_control(
            session_scope, kind="interrupt", timeout=timeout
        )

    async def _request_cancel_active(
        self, active: _ActiveExecution, timeout: float = 2.0
    ) -> None:
        """Request cancellation for an active execution and wait briefly for shutdown."""
        active.cancel_requested = True
        try:
            await active.client.interrupt()
        except Exception:
            pass

        try:
            await asyncio.wait_for(active.done_event.wait(), timeout=timeout)
            return
        except asyncio.TimeoutError:
            pass

        try:
            await active.client.disconnect()
        except Exception:
            pass

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution by execution identifier."""
        async with self._active_lock:
            active = self._active_executions_by_execution_id.get(execution_id)
        if not active:
            return False
        await self._request_cancel_active(active)
        return True

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active execution for a channel/thread session scope."""
        async with self._active_lock:
            active = self._active_executions_by_scope.get(session_scope)
        if not active:
            return 0
        await self._request_cancel_active(active)
        return 1

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel."""
        async with self._active_lock:
            actives = [
                active
                for scope, active in self._active_executions_by_scope.items()
                if scope.startswith(f"{channel_id}:")
            ]

        for active in actives:
            await self._request_cancel_active(active)
        return len(actives)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        async with self._active_lock:
            actives = list(self._active_executions_by_scope.values())

        for active in actives:
            await self._request_cancel_active(active)
        return len(actives)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()

    async def _process_control_requests(
        self,
        *,
        active: _ActiveExecution,
        turn_counter: dict[str, int],
        turn_counter_lock: asyncio.Lock,
        log_prefix: str,
    ) -> None:
        """Process steer/interrupt requests for the active execution."""
        while not active.done_event.is_set():
            request = await active.control_queue.get()
            try:
                if request.kind == "steer":
                    await active.client.interrupt()
                    steer_text = request.text or ""
                    await active.client.query(
                        steer_text, session_id=active.query_session_id
                    )
                    async with turn_counter_lock:
                        turn_counter["pending"] += 1
                    result = TurnControlResult(
                        success=True,
                        session_id=active.session_id,
                    )
                elif request.kind == "interrupt":
                    await active.client.interrupt()
                    result = TurnControlResult(
                        success=True,
                        session_id=active.session_id,
                    )
                else:
                    result = TurnControlResult(
                        success=False, error=f"Unknown control kind: {request.kind}"
                    )

                if not request.future.done():
                    request.future.set_result(result)
            except Exception as e:
                logger.error(
                    f"{log_prefix}Failed processing Claude control request {request.kind}: {e}"
                )
                if not request.future.done():
                    request.future.set_result(
                        TurnControlResult(
                            success=False, error=str(e), session_id=active.session_id
                        )
                    )

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        on_tool_permission_request: Optional[
            Callable[[str, dict], Awaitable[bool]]
        ] = None,
        permission_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        _recursion_depth: int = 0,
        _is_retry_after_exit_plan_error: bool = False,
    ) -> ExecutionResult:
        """Execute a prompt via Claude SDK streaming mode."""
        log_prefix = self.build_log_prefix(db_session_id)

        retry_error = self.validate_retry_depth(_recursion_depth, log_prefix)
        if retry_error:
            return ExecutionResult(success=False, output="", error=retry_error)

        tracking = self.create_tracking_context(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        state = ExecutionState()
        async with self._states_lock:
            self._execution_states[tracking.track_id] = state

        options, initial_query_session_id = self._build_sdk_options(
            working_directory=working_directory,
            resume_session_id=resume_session_id,
            permission_mode=permission_mode,
            model=model,
            log_prefix=log_prefix,
            on_tool_permission_request=on_tool_permission_request,
        )

        prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(
            f"{log_prefix}Executing SDK query in {working_directory}: '{prompt_preview}'"
        )

        parser = StreamParser()
        accumulator = StreamAccumulator(join_assistant_chunks=concat_with_spacing)

        client = ClaudeSDKClient(options=options)
        active = _ActiveExecution(
            session_scope=tracking.session_scope,
            execution_id=execution_id or tracking.track_id,
            query_session_id=initial_query_session_id,
            client=client,
        )

        registered = False
        control_task: Optional[asyncio.Task] = None
        turn_counter = {"pending": 1}
        turn_counter_lock = asyncio.Lock()
        stop_after_result = False
        result_is_error = False
        partial_assistant_buffer = ""

        try:
            await client.connect()
            registered = await self._register_active_execution(active)
            if not registered:
                await client.disconnect()
                return ExecutionResult(
                    success=False,
                    output="",
                    error="Claude execution already active for this session scope",
                )

            control_task = asyncio.create_task(
                self._process_control_requests(
                    active=active,
                    turn_counter=turn_counter,
                    turn_counter_lock=turn_counter_lock,
                    log_prefix=log_prefix,
                )
            )

            await client.query(prompt, session_id=active.query_session_id)

            async for sdk_message in client.receive_messages():
                if isinstance(sdk_message, ResultMessage):
                    result_is_error = sdk_message.is_error
                    if sdk_message.session_id:
                        active.session_id = sdk_message.session_id
                        active.query_session_id = sdk_message.session_id

                raw_message = self._sdk_message_to_raw(sdk_message)
                if raw_message is None:
                    continue

                line_str = json.dumps(raw_message, ensure_ascii=False)
                msg = parser.parse_line(line_str)
                if not msg:
                    continue

                is_partial_assistant = (
                    msg.type == "assistant"
                    and bool(msg.raw and msg.raw.get("partial"))
                    and bool(msg.content)
                )
                if is_partial_assistant:
                    partial_assistant_buffer += msg.content
                elif (
                    msg.type == "assistant" and msg.content and partial_assistant_buffer
                ):
                    if msg.content.strip() == partial_assistant_buffer.strip():
                        logger.debug(
                            f"{log_prefix}Skipping duplicate final assistant chunk after partial stream"
                        )
                        partial_assistant_buffer = ""
                        continue
                    partial_assistant_buffer = ""
                elif msg.type == "result":
                    partial_assistant_buffer = ""

                if msg.session_id:
                    active.session_id = msg.session_id
                    active.query_session_id = msg.session_id

                if msg.type == "assistant":
                    if msg.content:
                        preview = (
                            msg.content[:100] + "..."
                            if len(msg.content) > 100
                            else msg.content
                        )
                        logger.debug(f"{log_prefix}Claude: {preview}")
                    if msg.raw:
                        message = msg.raw.get("message", {})
                        if isinstance(message, dict):
                            content_blocks = message.get("content", [])
                        else:
                            content_blocks = []
                        if not isinstance(content_blocks, list):
                            content_blocks = []

                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})

                                if tool_name in ("Read", "Edit", "Write"):
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(
                                        f"{log_prefix}Tool: {tool_name} {file_path}"
                                    )
                                    if tool_name in ("Write", "Edit") and file_path:
                                        in_plan_mode = state.exit_plan_mode_detected
                                        if not in_plan_mode:
                                            current_mode = (
                                                await self._get_current_permission_mode(
                                                    db_session_id, permission_mode
                                                )
                                            )
                                            in_plan_mode = current_mode == "plan"
                                        if in_plan_mode and file_path.endswith(".md"):
                                            tool_id = block.get("id")
                                            if (
                                                tool_id
                                                and tool_id
                                                not in state.pending_write_tools
                                            ):
                                                state.pending_write_tools[tool_id] = (
                                                    file_path
                                                )
                                                logger.info(
                                                    f"{log_prefix}Tracking pending plan write: {file_path}"
                                                )
                                elif tool_name == "Bash":
                                    command = tool_input.get("command", "")[:50]
                                    logger.info(
                                        f"{log_prefix}Tool: Bash '{command}...'"
                                    )
                                elif tool_name == "AskUserQuestion":
                                    questions = tool_input.get("questions", [])
                                    if questions:
                                        first_q = questions[0].get("question", "?")[:80]
                                        logger.info(
                                            f"{log_prefix}Tool: AskUserQuestion - '{first_q}...' ({len(questions)} question(s))"
                                        )
                                    else:
                                        logger.info(
                                            f"{log_prefix}Tool: AskUserQuestion"
                                        )
                                    state.ask_user_question_detected = True
                                elif tool_name == "ExitPlanMode":
                                    state.exit_plan_mode_tool_id = block.get("id")
                                    state.exit_plan_mode_detected = True
                                    if state.exit_plan_mode_detected_at is None:
                                        state.exit_plan_mode_detected_at = (
                                            time.monotonic()
                                        )
                                    logger.info(
                                        f"{log_prefix}Tool: ExitPlanMode - will terminate for Slack approval"
                                    )
                                elif tool_name == "Task":
                                    subagent_type = tool_input.get("subagent_type", "")
                                    desc = tool_input.get("description", "")[:50]
                                    should_track = subagent_type == "Plan"
                                    if not should_track:
                                        current_mode = (
                                            await self._get_current_permission_mode(
                                                db_session_id, permission_mode
                                            )
                                        )
                                        should_track = current_mode == "plan"
                                    if should_track:
                                        state.plan_subagent_tool_id = block.get("id")
                                        state.plan_subagent_is_plan_type = (
                                            subagent_type == "Plan"
                                        )
                                        state.plan_subagent_completed = False
                                        state.plan_subagent_completed_at = None
                                        logger.info(
                                            f"{log_prefix}Tool: Task (subagent_type={subagent_type or 'default'}) '{desc}...' - tracking for plan approval"
                                        )
                                    else:
                                        logger.info(
                                            f"{log_prefix}Tool: Task '{desc}...'"
                                        )
                                else:
                                    logger.info(f"{log_prefix}Tool: {tool_name}")
                elif msg.type == "user" and msg.raw:
                    message = msg.raw.get("message", {})
                    if isinstance(message, dict):
                        content_blocks = message.get("content", [])
                    else:
                        content_blocks = []
                    if not isinstance(content_blocks, list):
                        content_blocks = []

                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue

                        tool_use_id = block.get("tool_use_id", "")
                        is_error = block.get("is_error", False)

                        if (
                            is_error
                            and state.exit_plan_mode_tool_id
                            and tool_use_id == state.exit_plan_mode_tool_id
                            and state.exit_plan_mode_detected
                            and not _is_retry_after_exit_plan_error
                        ):
                            logger.warning(
                                f"{log_prefix}ExitPlanMode failed - will retry with bypass mode"
                            )
                            state.exit_plan_mode_error_detected = True

                        if (
                            not is_error
                            and state.plan_subagent_tool_id
                            and tool_use_id == state.plan_subagent_tool_id
                        ):
                            state.plan_subagent_completed = True
                            state.plan_subagent_completed_at = time.monotonic()
                            if state.plan_subagent_is_plan_type:
                                result_content = block.get("content", [])
                                if isinstance(result_content, list):
                                    for content_block in result_content:
                                        if not isinstance(content_block, dict):
                                            continue
                                        if content_block.get("type") == "text":
                                            state.plan_subagent_result = (
                                                content_block.get("text", "")
                                            )
                                            break
                                elif isinstance(result_content, str):
                                    state.plan_subagent_result = result_content

                        if tool_use_id in state.pending_write_tools:
                            file_path = state.pending_write_tools.pop(tool_use_id)
                            if not is_error:
                                state.plan_write_completed = True
                                state.plan_write_path = file_path

                accumulator.apply(msg)
                if on_chunk:
                    await on_chunk(msg)

                if state.exit_plan_mode_error_detected:
                    stop_after_result = True
                    await client.interrupt()

                if state.ask_user_question_detected and not stop_after_result:
                    stop_after_result = True
                    await client.interrupt()

                if state.exit_plan_mode_detected and not stop_after_result:
                    plan_subagent_pending = (
                        state.plan_subagent_tool_id
                        and not state.plan_subagent_completed
                    )
                    write_pending = bool(state.pending_write_tools)
                    if not plan_subagent_pending and not write_pending:
                        stop_after_result = True
                        await client.interrupt()
                    elif write_pending and state.exit_plan_mode_detected_at is not None:
                        elapsed = time.monotonic() - state.exit_plan_mode_detected_at
                        if elapsed > PLAN_WRITE_GRACE_SECONDS:
                            state.plan_write_timeout = True
                            stop_after_result = True
                            await client.interrupt()

                if (
                    state.plan_subagent_completed
                    and state.plan_subagent_is_plan_type
                    and not stop_after_result
                ):
                    if (
                        state.pending_write_tools
                        and state.plan_subagent_completed_at is not None
                    ):
                        elapsed = time.monotonic() - state.plan_subagent_completed_at
                        if elapsed > PLAN_WRITE_GRACE_SECONDS:
                            state.plan_write_timeout = True
                            stop_after_result = True
                            await client.interrupt()
                    elif not state.pending_write_tools:
                        if state.plan_write_completed:
                            stop_after_result = True
                            await client.interrupt()
                        elif state.plan_subagent_completed_at is not None:
                            elapsed = (
                                time.monotonic() - state.plan_subagent_completed_at
                            )
                            if elapsed > PLAN_WRITE_GRACE_SECONDS:
                                state.plan_write_timeout = True
                                stop_after_result = True
                                await client.interrupt()
                            elif not state.plan_write_wait_logged:
                                logger.info(
                                    f"{log_prefix}Plan subagent completed - waiting up to {PLAN_WRITE_GRACE_SECONDS:.0f}s for plan write to start/finish"
                                )
                                state.plan_write_wait_logged = True

                if isinstance(sdk_message, ResultMessage):
                    async with turn_counter_lock:
                        turn_counter["pending"] = max(0, turn_counter["pending"] - 1)
                        pending_turns = turn_counter["pending"]

                    if active.cancel_requested:
                        break
                    if stop_after_result and pending_turns <= 0:
                        break
                    if pending_turns <= 0 and active.control_queue.empty():
                        break

            success = (
                not result_is_error
                and not accumulator.error_message
                and not active.cancel_requested
            )

            if active.cancel_requested:
                return ExecutionResult(
                    **accumulator.result_fields(
                        success=False,
                        error="Cancelled",
                        was_cancelled=True,
                    ),
                    has_pending_question=False,
                    has_pending_plan_approval=False,
                    plan_subagent_result=state.plan_subagent_result,
                    plan_write_timeout=state.plan_write_timeout,
                )

            if (
                not success
                and resume_session_id
                and "No conversation found with session ID"
                in (accumulator.error_message or "")
            ):
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    on_tool_permission_request=on_tool_permission_request,
                    permission_mode=permission_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                    _is_retry_after_exit_plan_error=_is_retry_after_exit_plan_error,
                )

            if (
                state.exit_plan_mode_error_detected
                and not _is_retry_after_exit_plan_error
            ):
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=resume_session_id,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    on_tool_permission_request=on_tool_permission_request,
                    permission_mode=config.DEFAULT_BYPASS_MODE,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                    _is_retry_after_exit_plan_error=True,
                )

            has_plan_approval = state.exit_plan_mode_detected or (
                state.plan_subagent_completed and state.plan_subagent_is_plan_type
            )

            result_fields = accumulator.result_fields(success=success)
            return ExecutionResult(
                **result_fields,
                has_pending_question=state.ask_user_question_detected,
                has_pending_plan_approval=has_plan_approval,
                plan_subagent_result=state.plan_subagent_result,
                plan_write_timeout=state.plan_write_timeout,
            )

        except asyncio.CancelledError:
            active.cancel_requested = True
            try:
                await client.interrupt()
            except Exception:
                pass
            return ExecutionResult(
                **accumulator.result_fields(
                    success=False, error="Cancelled", was_cancelled=True
                ),
                has_pending_question=False,
                has_pending_plan_approval=False,
                plan_subagent_result=state.plan_subagent_result,
                plan_write_timeout=state.plan_write_timeout,
            )
        except Exception as e:
            logger.error(f"{log_prefix}Error during SDK execution: {e}")
            return ExecutionResult(
                **accumulator.result_fields(success=False, error=str(e)),
                has_pending_question=False,
                has_pending_plan_approval=False,
                plan_subagent_result=state.plan_subagent_result,
                plan_write_timeout=state.plan_write_timeout,
            )
        finally:
            active.done_event.set()
            if control_task:
                control_task.cancel()
                try:
                    await control_task
                except asyncio.CancelledError:
                    pass
            try:
                await client.disconnect()
            except Exception:
                pass

            if registered:
                await self._unregister_active_execution(active)

            async with self._states_lock:
                self._execution_states.pop(tracking.track_id, None)
