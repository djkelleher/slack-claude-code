"""Claude Code executor using the Claude Agent SDK (ClaudeSDKClient)."""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.backends.execution_result import BackendExecutionResult
from src.backends.stream_accumulator import StreamAccumulator
from src.utils.execution_scope import build_session_scope
from src.utils.stream_models import StreamMessage, concat_with_spacing

from ..config import config, parse_claude_model_effort
from .sdk_stream_adapter import SDKStreamAdapter

# Grace period to wait for plan writes after plan completion before interrupting.
PLAN_WRITE_GRACE_SECONDS = 600.0

# UUID pattern for validating session IDs
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

    from ..database.repository import DatabaseRepository


@dataclass
class ExecutionResult(BackendExecutionResult):
    """Result of a Claude SDK execution."""

    has_pending_question: bool = False
    has_pending_plan_approval: bool = False
    plan_subagent_result: Optional[str] = None
    plan_write_timeout: bool = False


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


@dataclass
class SteerResult:
    """Result of a steer attempt."""

    success: bool
    error: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass
class _ControlRequest:
    """Queued steer or interrupt request for an active turn."""

    kind: str  # "steer" or "interrupt"
    text: str = ""
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


@dataclass
class _ActiveTurnState:
    """Tracks one in-flight SDK execution per scope."""

    scope: str
    track_id: str
    session_id: Optional[str]
    control_queue: asyncio.Queue  # Queue[_ControlRequest]
    done_event: asyncio.Event
    client: Any  # ClaudeSDKClient
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class _PooledClient:
    """A cached ClaudeSDKClient with metadata."""

    client: Any  # ClaudeSDKClient
    scope: str
    last_activity: float = field(default_factory=time.monotonic)
    connected: bool = False


class SDKExecutor:
    """Execute Claude Code via the Claude Agent SDK.

    Manages a pool of ClaudeSDKClient instances, one per session scope.
    Supports persistent sessions, streaming, steer, and interrupt.
    """

    DEFAULT_MAX_RECURSION_DEPTH = 3

    def __init__(self, db: Optional["DatabaseRepository"] = None) -> None:
        self.db = db
        self._lock = asyncio.Lock()
        # Client pool: scope -> _PooledClient
        self._clients: dict[str, _PooledClient] = {}
        # Active turn tracking
        self._active_turns_by_scope: dict[str, _ActiveTurnState] = {}
        self._active_turns_by_track: dict[str, _ActiveTurnState] = {}
        self._active_turns_by_channel: dict[str, set[str]] = {}  # channel -> set of scopes
        # Idle janitor
        self._janitor_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public interface matching SubprocessExecutor
    # ------------------------------------------------------------------

    async def has_active_execution(self, session_scope: str) -> bool:
        """Return True when an execution is active for the session scope."""
        async with self._lock:
            return session_scope in self._active_turns_by_scope

    async def has_active_turn(self, session_scope: str) -> bool:
        """Alias for has_active_execution for Codex-style routing."""
        return await self.has_active_execution(session_scope)

    async def steer_active_turn(
        self,
        session_scope: str,
        text: str,
        timeout: float = 5.0,
    ) -> SteerResult:
        """Send a steer message to an active turn in the given scope."""
        async with self._lock:
            turn = self._active_turns_by_scope.get(session_scope)
        if not turn:
            return SteerResult(success=False, error="No active turn for scope")

        request = _ControlRequest(kind="steer", text=text)
        turn.control_queue.put_nowait(request)

        try:
            result = await asyncio.wait_for(request.future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return SteerResult(success=False, error="Steer request timed out")

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active executions in one session scope."""
        async with self._lock:
            turn = self._active_turns_by_scope.get(session_scope)
        if not turn:
            return 0

        request = _ControlRequest(kind="interrupt")
        turn.control_queue.put_nowait(request)
        try:
            await asyncio.wait_for(request.future, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(f"Interrupt timed out for scope {session_scope}")
        return 1

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel."""
        async with self._lock:
            scopes = list(self._active_turns_by_channel.get(channel_id, set()))
        cancelled = 0
        for scope in scopes:
            cancelled += await self.cancel_by_scope(scope)
        return cancelled

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        async with self._lock:
            scopes = list(self._active_turns_by_scope.keys())
        cancelled = 0
        for scope in scopes:
            cancelled += await self.cancel_by_scope(scope)
        return cancelled

    async def shutdown(self) -> None:
        """Disconnect all clients and cancel all active executions."""
        await self.cancel_all()
        if self._janitor_task and not self._janitor_task.done():
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for pooled in list(self._clients.values()):
                await self._disconnect_client(pooled)
            self._clients.clear()

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        permission_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        worktree_name: Optional[str] = None,
        _recursion_depth: int = 0,
        _is_retry_after_exit_plan_error: bool = False,
    ) -> ExecutionResult:
        """Execute a prompt via the Claude Agent SDK.

        Parameters
        ----------
        prompt : str
            The prompt to send to Claude.
        working_directory : str
            Directory to run Claude in.
        session_id : str, optional
            Identifier for this execution (for tracking).
        resume_session_id : str, optional
            Claude session ID to resume.
        execution_id : str, optional
            Unique ID for this execution (for cancellation).
        on_chunk : callable, optional
            Async callback for each streamed message.
        permission_mode : str, optional
            Permission mode override.
        db_session_id : int, optional
            Database session ID.
        model : str, optional
            Model to use.
        channel_id : str, optional
            Slack channel ID.
        thread_ts : str, optional
            Slack thread timestamp.
        worktree_name : str, optional
            Not used with SDK (Claude handles worktrees internally).
        _recursion_depth : int
            Internal retry depth counter.
        _is_retry_after_exit_plan_error : bool
            Whether this is a retry after ExitPlanMode error.

        Returns
        -------
        ExecutionResult
        """
        log_prefix = self._build_log_prefix(db_session_id)

        if _recursion_depth >= self.DEFAULT_MAX_RECURSION_DEPTH:
            logger.error(f"{log_prefix}Max recursion depth reached, aborting")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max retry depth ({self.DEFAULT_MAX_RECURSION_DEPTH}) exceeded",
            )

        session_scope = build_session_scope(channel_id or "", thread_ts)
        track_id = execution_id or session_id or session_scope

        # Resolve model and effort
        effective_model = model or config.DEFAULT_MODEL
        claude_model: Optional[str] = None
        claude_effort: Optional[str] = None
        if effective_model:
            claude_model, claude_effort = parse_claude_model_effort(effective_model)
            logger.info(f"{log_prefix}Using model={claude_model} effort={claude_effort}")

        # Resolve permission mode
        requested_mode = permission_mode or config.CLAUDE_PERMISSION_MODE
        if requested_mode in config.VALID_PERMISSION_MODES:
            effective_mode = requested_mode
        else:
            logger.warning(
                f"{log_prefix}Invalid permission mode: {requested_mode}, "
                f"using {config.DEFAULT_BYPASS_MODE}"
            )
            effective_mode = config.DEFAULT_BYPASS_MODE
        logger.info(f"{log_prefix}Using permission_mode={effective_mode}")

        # Resolve added dirs
        added_dirs = await self._get_session_added_dirs(db_session_id)

        # Resolve allowed tools
        allowed_tools: Optional[list[str]] = None
        if config.ALLOWED_TOOLS:
            allowed_tools = [t.strip() for t in config.ALLOWED_TOOLS.split(",") if t.strip()]
            logger.info(f"{log_prefix}Using allowed_tools={allowed_tools}")

        # Build SDK options
        from claude_agent_sdk import ClaudeAgentOptions

        options_kwargs: dict[str, Any] = {
            "cwd": working_directory,
            "permission_mode": effective_mode,
        }
        if claude_model:
            options_kwargs["model"] = claude_model
        if claude_effort:
            options_kwargs["effort"] = claude_effort
        if allowed_tools:
            options_kwargs["allowed_tools"] = allowed_tools
        if added_dirs:
            options_kwargs["add_dirs"] = added_dirs

        # Session resume: use the resume option if we have a valid session ID
        # and don't have an existing live client for this scope
        resume_id: Optional[str] = None
        if resume_session_id and UUID_PATTERN.match(resume_session_id):
            resume_id = resume_session_id
            logger.info(f"{log_prefix}Will resume session {resume_id}")

        # Check if we already have a live client for this scope
        async with self._lock:
            pooled = self._clients.get(session_scope)

        reuse_client = False
        if pooled and pooled.connected:
            reuse_client = True
            logger.info(f"{log_prefix}Reusing existing SDK client for scope {session_scope}")
        else:
            # Need a new client
            if resume_id:
                options_kwargs["resume"] = resume_id
            options = ClaudeAgentOptions(**options_kwargs)
            client = await self._create_and_connect_client(options, session_scope, log_prefix)
            if not client:
                return ExecutionResult(
                    success=False,
                    output="",
                    error="Failed to connect Claude SDK client",
                )

        if reuse_client:
            client = pooled.client
        else:
            # Store in pool
            async with self._lock:
                self._clients[session_scope] = _PooledClient(
                    client=client,
                    scope=session_scope,
                    connected=True,
                )

        # Ensure idle janitor is running
        self._ensure_janitor()

        # Set up execution state
        state = ExecutionState()
        adapter = SDKStreamAdapter()
        accumulator = StreamAccumulator(join_assistant_chunks=concat_with_spacing)
        control_queue: asyncio.Queue[_ControlRequest] = asyncio.Queue()
        done_event = asyncio.Event()

        active_turn = _ActiveTurnState(
            scope=session_scope,
            track_id=track_id,
            session_id=resume_id,
            control_queue=control_queue,
            done_event=done_event,
            client=client,
        )

        # Register active turn
        async with self._lock:
            self._active_turns_by_scope[session_scope] = active_turn
            self._active_turns_by_track[track_id] = active_turn
            if channel_id:
                self._active_turns_by_channel.setdefault(channel_id, set()).add(session_scope)

        prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"{log_prefix}Sending prompt via SDK: '{prompt_preview}'")

        try:
            # Send prompt
            await client.query(prompt)

            # Main receive loop: race between SDK messages and control requests
            result = await self._receive_loop(
                client=client,
                adapter=adapter,
                accumulator=accumulator,
                state=state,
                control_queue=control_queue,
                on_chunk=on_chunk,
                log_prefix=log_prefix,
                effective_mode=effective_mode,
                permission_mode=permission_mode,
                db_session_id=db_session_id,
                _is_retry_after_exit_plan_error=_is_retry_after_exit_plan_error,
            )

            if result is not None:
                # Early return from receive loop (e.g., interrupt)
                return result

            # Check for session-not-found error: retry without resume
            if (
                not accumulator.error_message
                or "No conversation found with session ID" not in accumulator.error_message
            ):
                pass  # No session error
            elif resume_session_id:
                logger.info(
                    f"{log_prefix}Session {resume_session_id} not found, retrying without resume "
                    f"(depth={_recursion_depth + 1})"
                )
                await self._disconnect_scope(session_scope)
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=permission_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                )

            # Check for ExitPlanMode error: retry with bypass mode
            if state.exit_plan_mode_error_detected and not _is_retry_after_exit_plan_error:
                logger.info(
                    f"{log_prefix}Retrying with bypass mode after ExitPlanMode error "
                    f"(depth={_recursion_depth + 1})"
                )
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=resume_session_id,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=config.DEFAULT_BYPASS_MODE,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                    _is_retry_after_exit_plan_error=True,
                )

            success = not accumulator.error_message
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
            try:
                await client.interrupt()
            except Exception:
                pass
            return ExecutionResult(
                **accumulator.result_fields(
                    success=False,
                    error="Cancelled",
                    was_cancelled=True,
                )
            )
        except Exception as e:
            logger.error(f"{log_prefix}Error during SDK execution: {e}")
            return ExecutionResult(**accumulator.result_fields(success=False, error=str(e)))
        finally:
            # Unregister active turn
            async with self._lock:
                self._active_turns_by_scope.pop(session_scope, None)
                self._active_turns_by_track.pop(track_id, None)
                if channel_id and channel_id in self._active_turns_by_channel:
                    self._active_turns_by_channel[channel_id].discard(session_scope)
                    if not self._active_turns_by_channel[channel_id]:
                        del self._active_turns_by_channel[channel_id]
                # Update last activity for pool
                pooled = self._clients.get(session_scope)
                if pooled:
                    pooled.last_activity = time.monotonic()

    # ------------------------------------------------------------------
    # Main receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(
        self,
        *,
        client: "ClaudeSDKClient",
        adapter: SDKStreamAdapter,
        accumulator: StreamAccumulator,
        state: ExecutionState,
        control_queue: asyncio.Queue,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]],
        log_prefix: str,
        effective_mode: str,
        permission_mode: Optional[str],
        db_session_id: Optional[int],
        _is_retry_after_exit_plan_error: bool,
    ) -> Optional[ExecutionResult]:
        """Process SDK messages and control requests until completion.

        Returns None on normal completion (caller builds result), or an
        ExecutionResult for early-termination scenarios.
        """
        from claude_agent_sdk.types import TextBlock, ToolUseBlock

        msg_iter = client.receive_response()
        msg_task: Optional[asyncio.Task] = None
        ctrl_task: Optional[asyncio.Task] = None

        try:
            while True:
                # Create tasks for the race
                if msg_task is None or msg_task.done():
                    msg_task = asyncio.create_task(self._next_message(msg_iter))
                if ctrl_task is None or ctrl_task.done():
                    ctrl_task = asyncio.create_task(control_queue.get())

                # Apply a grace-period timeout when we're waiting for plan artifacts
                wait_timeout = None
                if (
                    state.exit_plan_mode_detected
                    and not state.plan_subagent_completed
                    and not state.pending_write_tools
                ):
                    if state.exit_plan_mode_detected_at is None:
                        state.exit_plan_mode_detected_at = time.monotonic()
                    remaining = PLAN_WRITE_GRACE_SECONDS - (
                        time.monotonic() - state.exit_plan_mode_detected_at
                    )
                    if remaining <= 0:
                        logger.warning(
                            f"{log_prefix}ExitPlanMode detected but no plan artifacts; "
                            "interrupting after grace period"
                        )
                        state.plan_write_timeout = True
                        await client.interrupt()
                        break
                    wait_timeout = remaining

                try:
                    done, _pending = await asyncio.wait(
                        {msg_task, ctrl_task},
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=wait_timeout,
                    )
                except asyncio.CancelledError:
                    raise

                if not done:
                    # Timeout expired (grace period)
                    logger.warning(
                        f"{log_prefix}Grace period expired waiting for plan artifacts; interrupting"
                    )
                    state.plan_write_timeout = True
                    await client.interrupt()
                    break

                # Handle SDK message
                if msg_task in done:
                    sdk_message = msg_task.result()
                    if sdk_message is None:
                        # Iterator exhausted
                        break

                    msg = adapter.adapt(sdk_message)
                    if msg is None:
                        continue

                    # Log messages
                    self._log_message(msg, log_prefix)

                    # Detect tool signals from SDK message
                    self._detect_tool_signals(
                        sdk_message=sdk_message,
                        state=state,
                        effective_mode=effective_mode,
                        permission_mode=permission_mode,
                        db_session_id=db_session_id,
                        log_prefix=log_prefix,
                        _is_retry_after_exit_plan_error=_is_retry_after_exit_plan_error,
                        TextBlock=TextBlock,
                        ToolUseBlock=ToolUseBlock,
                    )

                    # Apply to accumulator
                    accumulator.apply(msg)

                    # Call chunk callback
                    if on_chunk:
                        await on_chunk(msg)

                    # Check early termination conditions
                    should_break = await self._check_early_termination(
                        client=client,
                        state=state,
                        accumulator=accumulator,
                        log_prefix=log_prefix,
                    )
                    if should_break:
                        break

                    if msg.is_final:
                        break

                # Handle control request
                if ctrl_task in done:
                    request: _ControlRequest = ctrl_task.result()
                    if request.kind == "steer":
                        try:
                            await client.query(request.text)
                            request.future.set_result(
                                SteerResult(success=True, message="Steer accepted")
                            )
                            logger.info(f"{log_prefix}Steer accepted")
                        except Exception as e:
                            request.future.set_result(SteerResult(success=False, error=str(e)))
                            logger.warning(f"{log_prefix}Steer failed: {e}")
                    elif request.kind == "interrupt":
                        try:
                            await client.interrupt()
                            request.future.set_result(
                                SteerResult(success=True, message="Interrupted")
                            )
                            logger.info(f"{log_prefix}Interrupt sent")
                        except Exception as e:
                            request.future.set_result(SteerResult(success=False, error=str(e)))
                        break
                    # Reset ctrl_task so a new one is created on next iteration
                    ctrl_task = None

        finally:
            # Cancel any pending tasks
            if msg_task and not msg_task.done():
                msg_task.cancel()
                try:
                    await msg_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            if ctrl_task and not ctrl_task.done():
                ctrl_task.cancel()
                try:
                    await ctrl_task
                except asyncio.CancelledError:
                    pass

        return None  # Normal completion

    @staticmethod
    async def _next_message(msg_iter: Any) -> Any:
        """Get the next message from an async iterator, returning None on exhaustion."""
        try:
            return await msg_iter.__anext__()
        except StopAsyncIteration:
            return None

    # ------------------------------------------------------------------
    # Tool signal detection
    # ------------------------------------------------------------------

    def _detect_tool_signals(
        self,
        *,
        sdk_message: Any,
        state: ExecutionState,
        effective_mode: str,
        permission_mode: Optional[str],
        db_session_id: Optional[int],
        log_prefix: str,
        _is_retry_after_exit_plan_error: bool,
        TextBlock: type,
        ToolUseBlock: type,
    ) -> None:
        """Inspect SDK messages for plan/question/task tool signals."""
        from claude_agent_sdk import (
            AssistantMessage,
            TaskNotificationMessage,
            TaskStartedMessage,
            UserMessage,
        )
        from claude_agent_sdk.types import ToolResultBlock

        if isinstance(sdk_message, AssistantMessage):
            for block in sdk_message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                tool_name = block.name
                tool_input = block.input if isinstance(block.input, dict) else {}
                tool_id = block.id

                if tool_name == "AskUserQuestion":
                    state.ask_user_question_detected = True
                    logger.info(
                        f"{log_prefix}AskUserQuestion detected - will terminate for Slack handling"
                    )

                elif tool_name == "ExitPlanMode":
                    if effective_mode == "plan":
                        state.exit_plan_mode_tool_id = tool_id
                        state.exit_plan_mode_detected = True
                        if state.exit_plan_mode_detected_at is None:
                            state.exit_plan_mode_detected_at = time.monotonic()
                        logger.info(
                            f"{log_prefix}ExitPlanMode detected - will terminate for Slack approval"
                        )

                elif tool_name == "Task":
                    subagent_type = tool_input.get("subagent_type", "")
                    should_track = subagent_type == "Plan" or effective_mode == "plan"
                    if should_track:
                        state.plan_subagent_tool_id = tool_id
                        state.plan_subagent_is_plan_type = subagent_type == "Plan"
                        state.plan_subagent_completed = False
                        state.plan_subagent_completed_at = None
                        desc = tool_input.get("description", "")[:50]
                        logger.info(
                            f"{log_prefix}Task (subagent_type={subagent_type or 'default'}) "
                            f"'{desc}...' - tracking for plan approval"
                        )

                elif tool_name in ("Write", "Edit"):
                    file_path = tool_input.get("file_path", "")
                    in_plan_mode = state.exit_plan_mode_detected or effective_mode == "plan"
                    if in_plan_mode and file_path and file_path.endswith(".md"):
                        if tool_id not in state.pending_write_tools:
                            state.pending_write_tools[tool_id] = file_path
                            logger.info(f"{log_prefix}Tracking pending plan write: {file_path}")

        elif isinstance(sdk_message, UserMessage):
            for block in sdk_message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                tool_use_id = block.tool_use_id
                is_error = block.is_error

                # Detect ExitPlanMode error for retry
                if (
                    is_error
                    and state.exit_plan_mode_tool_id
                    and tool_use_id == state.exit_plan_mode_tool_id
                    and state.exit_plan_mode_detected
                    and not _is_retry_after_exit_plan_error
                ):
                    logger.warning(f"{log_prefix}ExitPlanMode failed - will retry with bypass mode")
                    state.exit_plan_mode_error_detected = True

                # Detect Task completion in plan mode
                if (
                    not is_error
                    and state.plan_subagent_tool_id
                    and tool_use_id == state.plan_subagent_tool_id
                ):
                    state.plan_subagent_completed = True
                    state.plan_subagent_completed_at = time.monotonic()
                    logger.info(f"{log_prefix}Task tool completed in plan mode")

                    if state.plan_subagent_is_plan_type:
                        raw_content = block.content
                        if isinstance(raw_content, list):
                            for content_block in raw_content:
                                if (
                                    isinstance(content_block, dict)
                                    and content_block.get("type") == "text"
                                ):
                                    state.plan_subagent_result = content_block.get("text", "")
                                    break
                        elif isinstance(raw_content, str):
                            state.plan_subagent_result = raw_content

                # Track Write tool completion
                if tool_use_id in state.pending_write_tools:
                    file_path = state.pending_write_tools.pop(tool_use_id)
                    status = "ERROR" if is_error else "OK"
                    logger.info(f"{log_prefix}Write completed ({status}) for {file_path}")
                    if not is_error:
                        state.plan_write_completed = True
                        state.plan_write_path = file_path

        elif isinstance(sdk_message, TaskStartedMessage):
            subagent_type = sdk_message.task_type or ""
            should_track = subagent_type == "Plan" or effective_mode == "plan"
            if should_track:
                tool_id = sdk_message.tool_use_id or sdk_message.task_id or ""
                state.plan_subagent_tool_id = tool_id
                state.plan_subagent_is_plan_type = subagent_type == "Plan"
                state.plan_subagent_completed = False
                logger.info(f"{log_prefix}TaskStarted (type={subagent_type}) - tracking for plan")

        elif isinstance(sdk_message, TaskNotificationMessage):
            tool_id = sdk_message.tool_use_id or sdk_message.task_id or ""
            is_error = sdk_message.status in ("error", "failed")
            if state.plan_subagent_tool_id and tool_id == state.plan_subagent_tool_id:
                state.plan_subagent_completed = True
                state.plan_subagent_completed_at = time.monotonic()
                if state.plan_subagent_is_plan_type and not is_error:
                    state.plan_subagent_result = sdk_message.summary or ""
                logger.info(f"{log_prefix}TaskNotification completed for plan subagent")

    # ------------------------------------------------------------------
    # Early termination checks
    # ------------------------------------------------------------------

    async def _check_early_termination(
        self,
        *,
        client: "ClaudeSDKClient",
        state: ExecutionState,
        accumulator: StreamAccumulator,
        log_prefix: str,
    ) -> bool:
        """Return True when the receive loop should break early."""
        # ExitPlanMode error -> break to retry
        if state.exit_plan_mode_error_detected:
            logger.info(f"{log_prefix}Terminating to retry without plan mode")
            await client.interrupt()
            return True

        # AskUserQuestion -> break to handle in Slack
        if state.ask_user_question_detected:
            logger.info(f"{log_prefix}Terminating to handle AskUserQuestion in Slack")
            await client.interrupt()
            return True

        # ExitPlanMode detected in plan mode
        if state.exit_plan_mode_detected:
            plan_subagent_pending = (
                state.plan_subagent_tool_id and not state.plan_subagent_completed
            )
            write_pending = bool(state.pending_write_tools)
            if plan_subagent_pending or write_pending:
                # Check grace period for writes
                if write_pending and state.exit_plan_mode_detected_at is not None:
                    elapsed = time.monotonic() - state.exit_plan_mode_detected_at
                    if elapsed > PLAN_WRITE_GRACE_SECONDS:
                        logger.warning(
                            f"{log_prefix}Write tools still pending after {elapsed:.1f}s; "
                            "interrupting anyway"
                        )
                        await client.interrupt()
                        return True
                # Continue waiting for subagent/writes
                return False
            logger.info(f"{log_prefix}Terminating for plan approval in Slack")
            await client.interrupt()
            return True

        # Plan subagent (type=Plan) completed
        if state.plan_subagent_completed and state.plan_subagent_is_plan_type:
            if state.pending_write_tools:
                if state.plan_subagent_completed_at is not None:
                    elapsed = time.monotonic() - state.plan_subagent_completed_at
                    if elapsed > PLAN_WRITE_GRACE_SECONDS:
                        await client.interrupt()
                        return True
                return False
            if state.plan_write_completed:
                logger.info(
                    f"{log_prefix}Plan write completed ({state.plan_write_path}); "
                    "terminating for approval"
                )
                await client.interrupt()
                return True
            if state.plan_subagent_completed_at is not None:
                elapsed = time.monotonic() - state.plan_subagent_completed_at
                if elapsed > PLAN_WRITE_GRACE_SECONDS:
                    logger.warning(f"{log_prefix}No plan write after {elapsed:.1f}s; terminating")
                    await client.interrupt()
                    return True
            if not state.plan_write_wait_logged:
                logger.info(f"{log_prefix}Plan subagent completed - waiting for plan write")
                state.plan_write_wait_logged = True
            return False

        return False

    # ------------------------------------------------------------------
    # Client pool management
    # ------------------------------------------------------------------

    async def _create_and_connect_client(
        self,
        options: Any,
        session_scope: str,
        log_prefix: str,
    ) -> Optional["ClaudeSDKClient"]:
        """Create and connect a new ClaudeSDKClient."""
        from claude_agent_sdk import ClaudeSDKClient

        try:
            client = ClaudeSDKClient(options=options)
            await client.connect()
            logger.info(f"{log_prefix}SDK client connected for scope {session_scope}")
            return client
        except Exception as e:
            logger.error(f"{log_prefix}Failed to connect SDK client: {e}")
            return None

    async def _disconnect_client(self, pooled: _PooledClient) -> None:
        """Disconnect a pooled client safely."""
        pooled.connected = False
        try:
            await pooled.client.disconnect()
        except Exception as e:
            logger.debug(f"Error disconnecting client for {pooled.scope}: {e}")

    async def _disconnect_scope(self, session_scope: str) -> None:
        """Disconnect and remove the client for a scope."""
        async with self._lock:
            pooled = self._clients.pop(session_scope, None)
        if pooled:
            await self._disconnect_client(pooled)

    # ------------------------------------------------------------------
    # Idle janitor
    # ------------------------------------------------------------------

    def _ensure_janitor(self) -> None:
        """Start the idle janitor background task if not already running."""
        if self._janitor_task and not self._janitor_task.done():
            return
        self._janitor_task = asyncio.create_task(self._idle_janitor_loop())

    async def _idle_janitor_loop(self) -> None:
        """Periodically disconnect idle clients."""
        idle_timeout = getattr(config, "CLAUDE_SDK_IDLE_TIMEOUT_SECONDS", 900)
        interval = 30.0
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                to_disconnect: list[_PooledClient] = []
                async with self._lock:
                    for scope, pooled in list(self._clients.items()):
                        if scope in self._active_turns_by_scope:
                            continue  # Don't disconnect active turns
                        if now - pooled.last_activity > idle_timeout:
                            to_disconnect.append(self._clients.pop(scope))
                for pooled in to_disconnect:
                    logger.info(f"Disconnecting idle SDK client for {pooled.scope}")
                    await self._disconnect_client(pooled)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_session_added_dirs(self, db_session_id: Optional[int]) -> list[str]:
        """Return session-added directories from the database."""
        if not self.db or not db_session_id:
            return []
        session = await self.db.get_session_by_id(db_session_id)
        if not session:
            return []
        return [str(path).strip() for path in session.added_dirs if str(path).strip()]

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
    def _build_log_prefix(db_session_id: Optional[int]) -> str:
        """Build a consistent session-aware logging prefix."""
        return f"[S:{db_session_id}] " if db_session_id else ""

    @staticmethod
    def _log_message(msg: StreamMessage, log_prefix: str) -> None:
        """Log a human-readable summary of a stream message."""
        if msg.type == "assistant" and msg.content:
            preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            logger.debug(f"{log_prefix}Claude: {preview}")
        elif msg.type == "init":
            logger.info(f"{log_prefix}Session initialized: {msg.session_id}")
        elif msg.type == "error":
            logger.error(f"{log_prefix}Error: {msg.content}")
        elif msg.type == "result":
            if msg.cost_usd:
                logger.info(
                    f"{log_prefix}Claude Finished - {msg.duration_ms}ms, ${msg.cost_usd:.4f}"
                )
            else:
                logger.info(f"{log_prefix}Claude Finished - {msg.duration_ms}ms")

        if msg.tool_activities:
            for tool in msg.tool_activities:
                if tool.name in ("Read", "Edit", "Write"):
                    file_path = tool.input.get("file_path", "")
                    logger.info(f"{log_prefix}Tool: {tool.name} {file_path}")
                elif tool.name == "Bash":
                    command = tool.input.get("command", "")[:50]
                    logger.info(f"{log_prefix}Tool: Bash '{command}...'")
                elif tool.result is not None:
                    status = "ERROR" if tool.is_error else "OK"
                    logger.info(f"{log_prefix}Tool result [{tool.id[:8]}]: {status}")
                else:
                    logger.info(f"{log_prefix}Tool: {tool.name}")
