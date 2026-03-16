"""Unit tests for queue processing handlers."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import config
from src.database.models import Session
from src.handlers.claude.queue import (
    _QUEUE_START_LOCKS,
    _execute_queue_item,
    _process_queue,
    _process_queue_scheduled_events,
    _queue_task_id,
    ensure_queue_processor,
    register_queue_commands,
)


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _queue_item(item_id: int, prompt: str, working_directory_override: str | None = None):
    """Build a queue-item-like namespace for tests."""
    return SimpleNamespace(
        id=item_id,
        prompt=prompt,
        working_directory_override=working_directory_override,
        parallel_group_id=None,
        parallel_limit=None,
    )


def _queue_control(state: str = "running"):
    """Build a queue-control-like namespace for tests."""
    return SimpleNamespace(state=state)


def _registered_handler(command_name: str, db):
    """Register queue commands and return a single command handler + deps wrapper."""
    app = _FakeApp()
    deps = SimpleNamespace(db=db)
    register_queue_commands(app, deps)
    return app.handlers[command_name], deps


async def _invoke_slash_handler(
    handler,
    *,
    command_name: str,
    text: str = "",
    client: SimpleNamespace | None = None,
    thread_ts: str | None = None,
):
    """Invoke a slash-command handler with boilerplate test payload."""
    slack_client = client or SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    command = {
        "channel_id": "C123",
        "user_id": "U123",
        "text": text,
        "command": command_name,
    }
    if thread_ts is not None:
        command["thread_ts"] = thread_ts
    await handler(
        ack=AsyncMock(),
        command=command,
        client=slack_client,
        logger=MagicMock(),
    )
    return slack_client


@pytest.mark.asyncio
async def test_process_queue_marks_failed_when_initial_notification_fails():
    """Queue item should fail instead of staying running if initial Slack post fails."""
    item = _queue_item(42, "run analysis")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(side_effect=[Exception("slack unavailable"), {"ts": "999.001"}]),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.claude.queue.execute_for_session", new=AsyncMock()) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (42, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (42, "failed")
    assert (
        deps.db.update_queue_item_status.await_args_list[1].kwargs["error_message"]
        == "slack unavailable"
    )
    mock_execute.assert_not_awaited()
    client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_process_queue_skips_item_if_it_is_removed_before_claim():
    """Queue worker should skip execution when pending->running claim fails."""
    item = _queue_item(43, "run analysis")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(return_value=False),
            get_or_create_session=AsyncMock(),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.claude.queue.execute_for_session", new=AsyncMock()) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    deps.db.update_queue_item_status.assert_awaited_once_with(43, "running")
    mock_execute.assert_not_awaited()
    client.chat_postMessage.assert_not_called()
    client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_process_queue_completes_item_and_updates_message():
    """Successful queue item execution should complete and update Slack message."""
    item = _queue_item(7, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (7, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (7, "completed")
    assert deps.db.update_queue_item_status.await_args_list[1].kwargs["output"] == "done"
    assert (
        client.chat_postMessage.await_args_list[0].kwargs["text"]
        == "Processing queue item 1: run tests"
    )
    assert client.chat_postMessage.await_args_list[-1].kwargs["text"] == (
        "Queue finished: processed 1 item(s) (1 completed)."
    )
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_completion_update_failure_keeps_completed_status():
    """Streaming finalization failures should not flip successful item to failed."""
    item = _queue_item(71, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(side_effect=Exception("slack update failed")),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "completed"]
    assert client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_process_queue_failure_notification_error_does_not_crash_worker():
    """Slack notification failures in exception path should be logged, not raised."""
    item = _queue_item(72, "run tests")
    session = SimpleNamespace(id=1)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(side_effect=Exception("slack update failed")),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=Exception("execution failed")),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "failed"]
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_streams_updates_during_execution():
    """Queue item execution should stream intermediate output updates."""
    item = _queue_item(70, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    async def fake_execute_for_session(**kwargs):
        await kwargs["on_chunk"](
            SimpleNamespace(type="assistant", content="partial output", tool_activities=[])
        )
        return route_result

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=fake_execute_for_session),
    ) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert mock_execute.await_args.kwargs["on_chunk"] is not None
    assert (
        mock_execute.await_args.kwargs["auto_approve_permissions"]
        == config.QUEUE_AUTO_APPROVE_PERMISSIONS
    )
    assert client.chat_update.await_count >= 2


@pytest.mark.asyncio
async def test_execute_queue_item_routes_known_slash_command_through_router():
    """Queued slash commands should execute through slash handler dispatch."""
    item = _queue_item(73, "/clear")
    session = Session(id=1, channel_id="C123", model="opus")
    slash_router = SimpleNamespace(
        has_command=MagicMock(return_value=True),
        dispatch=AsyncMock(return_value=True),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(update_queue_item_status=AsyncMock(side_effect=[True, None])),
        codex_executor=None,
        slash_command_router=slash_router,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.claude.queue.execute_for_session", new=AsyncMock()) as mock_execute:
        result = await _execute_queue_item(
            item,
            channel_id="C123",
            thread_ts="123.456",
            scope="C123:123.456",
            deps=deps,
            client=client,
            log=MagicMock(),
            base_session=session,
            sequence_label="1",
            override_resume_ids={},
        )

    assert result == "completed"
    mock_execute.assert_not_awaited()
    slash_router.has_command.assert_called_once_with("/clear")
    slash_router.dispatch.assert_awaited_once()
    dispatch_kwargs = slash_router.dispatch.await_args.kwargs
    assert dispatch_kwargs["command_name"] == "/clear"
    assert dispatch_kwargs["command_text"] == ""
    assert dispatch_kwargs["channel_id"] == "C123"
    assert dispatch_kwargs["thread_ts"] == "123.456"
    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "completed"]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_queue_waits_for_active_codex_turn():
    """Queue processor should wait while active Codex turn is in progress for the same scope."""
    item = _queue_item(8, "follow up")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(result=SimpleNamespace(success=True, output="ok", error=None))
    codex_executor = SimpleNamespace(has_active_turn=AsyncMock(side_effect=[True, False, False]))
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=codex_executor,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock(), thread_ts="123.4")

    assert codex_executor.has_active_turn.await_count >= 2


@pytest.mark.asyncio
async def test_process_queue_recovers_from_transient_scope_error():
    """Scope-level errors should be logged and retried without crashing worker."""
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[Exception("db hiccup"), []]),
            update_queue_item_status=AsyncMock(),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    fake_logger = MagicMock()

    with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
        await _process_queue("C123", deps, client, fake_logger)

    assert deps.db.get_pending_queue_items.await_count == 2
    deps.db.update_queue_item_status.assert_not_called()
    client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_queue_processor_startup_is_serialized():
    """Concurrent startup checks should create only one processor task per scope."""
    deps = SimpleNamespace()
    client = SimpleNamespace()
    created_state = {"running": False}

    async def fake_is_running(*args, **kwargs):
        return created_state["running"]

    create_calls = 0

    async def fake_create_task(coro, *args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        # Simulate work that would expose races without per-scope locking.
        await asyncio.sleep(0.01)
        created_state["running"] = True
        coro.close()
        return AsyncMock()

    with patch(
        "src.handlers.claude.queue._is_queue_processor_running",
        new=AsyncMock(side_effect=fake_is_running),
    ):
        with patch(
            "src.handlers.claude.queue._create_queue_task",
            new=AsyncMock(side_effect=fake_create_task),
        ):
            await asyncio.gather(
                ensure_queue_processor("C123", "123.4", deps, client, MagicMock()),
                ensure_queue_processor("C123", "123.4", deps, client, MagicMock()),
            )

    assert create_calls == 1


@pytest.mark.asyncio
async def test_process_queue_cleans_scope_start_lock_on_exit():
    """Queue processor should clean up idle startup lock entries when exiting."""
    _QUEUE_START_LOCKS.clear()
    thread_ts = "123.4"
    task_id = _queue_task_id("C123", thread_ts)
    _QUEUE_START_LOCKS[task_id] = asyncio.Lock()

    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        chat_update=AsyncMock(),
    )

    await _process_queue("C123", deps, client, MagicMock(), thread_ts=thread_ts)

    assert task_id not in _QUEUE_START_LOCKS


@pytest.mark.asyncio
async def test_process_queue_cancelled_marks_running_item_cancelled():
    """Cancellation while running should transition current queue item to cancelled."""
    item = _queue_item(9, "long job")
    session = SimpleNamespace(id=1)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item]]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(side_effect=[_queue_control(), _queue_control("stopped")]),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "222.333"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (9, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (9, "cancelled")


@pytest.mark.asyncio
async def test_process_queue_pause_stops_before_next_item():
    """Paused queues should finish active work and leave later items pending."""
    item1 = _queue_item(21, "first task")
    item2 = _queue_item(22, "second task")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(result=SimpleNamespace(success=True, output="done", error=None))
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item1], [item2], [item2]]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(
                side_effect=[
                    _queue_control(),
                    _queue_control("paused"),
                    _queue_control("paused"),
                ]
            ),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "completed"]
    assert client.chat_postMessage.await_args_list[-1].kwargs["text"] == (
        "Queue paused: processed 1 item(s) (1 completed). 1 item(s) remain queued."
    )


@pytest.mark.asyncio
async def test_scheduled_queue_dispatcher_applies_resume_event():
    """Due scheduled resume event should flip state and restart pending queue work."""
    event = SimpleNamespace(
        id=501,
        channel_id="C123",
        thread_ts=None,
        action="resume",
        execute_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_due_queue_scheduled_events=AsyncMock(side_effect=[[event], []]),
            update_queue_control_state=AsyncMock(return_value=_queue_control("running")),
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=7)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            mark_queue_scheduled_event_executed=AsyncMock(return_value=True),
            mark_queue_scheduled_event_failed=AsyncMock(return_value=False),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    with patch(
        "src.handlers.claude.queue.ensure_queue_processor",
        new=AsyncMock(),
    ) as mock_ensure_queue:
        with patch(
            "src.handlers.claude.queue._recover_stale_running_items",
            new=AsyncMock(return_value=0),
        ):
            with patch(
                "src.handlers.claude.queue.asyncio.sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await _process_queue_scheduled_events(deps, client, MagicMock())

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", None, "running")
    deps.db.mark_queue_scheduled_event_executed.assert_awaited_once_with(501)
    deps.db.mark_queue_scheduled_event_failed.assert_not_awaited()
    mock_ensure_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduled_queue_dispatcher_marks_failed_event():
    """Unsupported scheduled action should be marked failed and not executed."""
    event = SimpleNamespace(
        id=777,
        channel_id="C123",
        thread_ts="123.456",
        action="unknown",
        execute_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_due_queue_scheduled_events=AsyncMock(side_effect=[[event], []]),
            mark_queue_scheduled_event_executed=AsyncMock(return_value=False),
            mark_queue_scheduled_event_failed=AsyncMock(return_value=True),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    with patch(
        "src.handlers.claude.queue.asyncio.sleep",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _process_queue_scheduled_events(deps, client, MagicMock())

    deps.db.mark_queue_scheduled_event_executed.assert_not_awaited()
    deps.db.mark_queue_scheduled_event_failed.assert_awaited_once()
    error_text = deps.db.mark_queue_scheduled_event_failed.await_args.args[1]
    assert "Unsupported scheduled queue action" in error_text


@pytest.mark.asyncio
async def test_execute_queue_item_plan_approval_posts_implementation_message():
    """Approved-plan handoff should post a new implementation processing message."""
    item = _queue_item(88, "ship fix")
    session = Session(id=1, channel_id="C123", model="opus")
    deps = SimpleNamespace(
        db=SimpleNamespace(update_queue_item_status=AsyncMock(side_effect=[True, None])),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(side_effect=[{"ts": "123.001"}, {"ts": "123.002"}]),
        chat_update=AsyncMock(),
    )

    class _FakeStreamingState:
        def __init__(self, **kwargs):
            self.message_ts = kwargs["message_ts"]
            self.accumulated_output = ""

        def start_heartbeat(self):
            return None

        async def finalize(self, is_error: bool = False):
            return None

        async def stop_heartbeat(self):
            return None

    async def _fake_execute_for_session(**kwargs):
        replacement_callback = await kwargs["on_plan_approved"]()
        assert callable(replacement_callback)
        return SimpleNamespace(
            backend="claude",
            result=SimpleNamespace(success=True, output="done", error=None, session_id=None),
        )

    with patch("src.handlers.claude.queue.StreamingMessageState", _FakeStreamingState):
        with patch(
            "src.handlers.claude.queue.create_streaming_callback",
            side_effect=lambda _state: AsyncMock(),
        ) as mock_callback_factory:
            with patch(
                "src.handlers.claude.queue.execute_for_session",
                new=AsyncMock(side_effect=_fake_execute_for_session),
            ):
                result = await _execute_queue_item(
                    item,
                    channel_id="C123",
                    thread_ts=None,
                    scope="C123:channel",
                    deps=deps,
                    client=client,
                    log=MagicMock(),
                    base_session=session,
                    sequence_label="1",
                    override_resume_ids={},
                )

    assert result == "completed"
    assert client.chat_postMessage.await_count == 2
    second_message = client.chat_postMessage.await_args_list[1].kwargs
    assert (
        second_message["text"] == "Processing queue item 1: ship fix (implementing approved plan)"
    )
    assert "Plan approved" in second_message["blocks"][0]["text"]["text"]
    assert mock_callback_factory.call_count == 2
    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "completed"]


@pytest.mark.asyncio
async def test_register_queue_commands_exposes_current_queue_commands():
    """Queue command registration should expose the current queue command set."""
    app = _FakeApp()
    register_queue_commands(app, SimpleNamespace(db=SimpleNamespace()))

    assert "/q" in app.handlers
    assert "/qc" in app.handlers
    assert "/qv" in app.handlers
    assert "/qclear" in app.handlers
    assert "/qdelete" in app.handlers
    assert "/qr" in app.handlers


@pytest.mark.asyncio
async def test_qv_posts_queue_status():
    """`/qv` should render queue state."""
    handler, deps = _registered_handler(
        "/qv",
        SimpleNamespace(
            list_queue_scopes_for_channel=AsyncMock(return_value=[]),
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_pending_queue_scheduled_events=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
    )
    client = await _invoke_slash_handler(handler, command_name="/qv")

    deps.db.list_queue_scopes_for_channel.assert_awaited_once_with("C123")
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"


@pytest.mark.asyncio
async def test_qc_view_subcommand_posts_channel_overview_without_thread_context():
    """`/qc view` should render a channel overview when no thread context exists."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            list_queue_scopes_for_channel=AsyncMock(return_value=["123.456"]),
            get_pending_queue_items=AsyncMock(side_effect=[[], [_queue_item(10, "pending")]]),
            get_running_queue_items=AsyncMock(side_effect=[[], [_queue_item(11, "running")]]),
            get_pending_queue_scheduled_events=AsyncMock(side_effect=[[], []]),
            get_queue_control=AsyncMock(side_effect=[_queue_control(), _queue_control("paused")]),
        ),
    )
    client = await _invoke_slash_handler(handler, command_name="/qc", text="view")

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"
    blocks = kwargs["blocks"]
    assert any("Thread 123.456" in block.get("text", {}).get("text", "") for block in blocks)


@pytest.mark.asyncio
async def test_qc_view_subcommand_accepts_explicit_thread_scope():
    """`/qc view <thread_ts>` should render that thread queue even without thread context."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_running_queue_items=AsyncMock(return_value=[_queue_item(12, "running")]),
            get_pending_queue_scheduled_events=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control("paused")),
        ),
    )
    client = await _invoke_slash_handler(handler, command_name="/qc", text="view 123.456")

    deps.db.get_pending_queue_items.assert_awaited_once_with("C123", "123.456")
    deps.db.get_running_queue_items.assert_awaited_once_with("C123", "123.456")
    deps.db.get_queue_control.assert_awaited_once_with("C123", "123.456")
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"


@pytest.mark.asyncio
async def test_qc_pause_updates_control_state():
    """`/qc pause` should persist paused queue state."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            get_running_queue_items=AsyncMock(return_value=[SimpleNamespace(id=1)]),
            update_queue_control_state=AsyncMock(return_value=_queue_control("paused")),
        ),
    )
    client = await _invoke_slash_handler(handler, command_name="/qc", text="pause")

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", None, "paused")
    assert client.chat_postMessage.await_args.kwargs["text"].startswith("Channel queue: pause")


@pytest.mark.asyncio
async def test_qc_stop_cancels_running_processor():
    """`/qc stop` should persist stopped state and cancel the worker."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            update_queue_control_state=AsyncMock(return_value=_queue_control("stopped"))
        ),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    with patch(
        "src.handlers.claude.queue.TaskManager.cancel", new=AsyncMock(return_value=True)
    ) as mock_cancel:
        await _invoke_slash_handler(handler, command_name="/qc", text="stop", client=client)

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", None, "stopped")
    mock_cancel.assert_awaited_once_with(_queue_task_id("C123", None))
    assert (
        client.chat_postMessage.await_args.kwargs["text"] == "Channel queue: stopped immediately."
    )


@pytest.mark.asyncio
async def test_qc_resume_restarts_pending_queue():
    """`/qc resume` should flip the queue back to running and restart processing."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            update_queue_control_state=AsyncMock(return_value=_queue_control("running")),
            get_pending_queue_items=AsyncMock(
                return_value=[SimpleNamespace(id=11), SimpleNamespace(id=12)]
            ),
            get_running_queue_items=AsyncMock(return_value=[]),
        ),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
        await _invoke_slash_handler(handler, command_name="/qc", text="resume", client=client)

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", None, "running")
    mock_ensure.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["text"] == (
        "Channel queue: resumed. 2 pending item(s) ready to run."
    )


@pytest.mark.asyncio
async def test_qc_resume_recovers_stale_running_items_and_restarts_pending_queue():
    """`/qc resume` should recover stale running rows and start pending work."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            update_queue_control_state=AsyncMock(return_value=_queue_control("running")),
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=21)]),
            get_running_queue_items=AsyncMock(side_effect=[[SimpleNamespace(id=88)], []]),
            update_queue_item_status=AsyncMock(return_value=True),
        ),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    with patch(
        "src.handlers.claude.queue._is_queue_processor_running",
        new=AsyncMock(return_value=False),
    ):
        with patch(
            "src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()
        ) as mock_ensure:
            await _invoke_slash_handler(handler, command_name="/qc", text="resume", client=client)

    deps.db.update_queue_item_status.assert_awaited_once_with(
        88,
        "cancelled",
        error_message="Recovered stale running queue item (no active queue processor).",
    )
    mock_ensure.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["text"] == (
        "Channel queue: resumed. 1 pending item(s) ready to run. "
        "Recovered 1 stale running item(s)."
    )


@pytest.mark.asyncio
async def test_qc_stop_accepts_explicit_thread_scope():
    """`/qc stop <thread_ts>` should target that thread queue."""
    handler, deps = _registered_handler(
        "/qc",
        SimpleNamespace(
            update_queue_control_state=AsyncMock(return_value=_queue_control("stopped"))
        ),
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    with patch(
        "src.handlers.claude.queue.TaskManager.cancel", new=AsyncMock(return_value=True)
    ) as mock_cancel:
        await _invoke_slash_handler(handler, command_name="/qc", text="stop 123.456", client=client)

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", "123.456", "stopped")
    mock_cancel.assert_awaited_once_with(_queue_task_id("C123", "123.456"))
    assert (
        client.chat_postMessage.await_args.kwargs["text"] == "Thread 123.456: stopped immediately."
    )


@pytest.mark.asyncio
async def test_qr_without_id_removes_next_pending_item():
    """`/qr` should remove the next pending queue item."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=11)]),
            remove_queue_item=AsyncMock(return_value=True),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qr"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/qr",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.remove_queue_item.assert_awaited_once_with(11, "C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Removed item #11 from queue"


@pytest.mark.asyncio
async def test_qc_remove_without_id_removes_next_pending_item():
    """`/qc remove` should remove the next pending queue item."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=12)]),
            remove_queue_item=AsyncMock(return_value=True),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qc"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "remove",
            "command": "/qc",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.remove_queue_item.assert_awaited_once_with(12, "C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Removed item #12 from queue"


@pytest.mark.asyncio
async def test_qclear_clears_pending_items():
    """`/qclear` should clear pending queue items."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            clear_queue=AsyncMock(return_value=3),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qclear"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/qclear",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.clear_queue.assert_awaited_once_with("C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Cleared 3 item(s) from queue"


@pytest.mark.asyncio
async def test_qdelete_deletes_entire_queue_scope():
    """`/qdelete` should remove all queue items and reset queue state."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            update_queue_control_state=AsyncMock(),
            delete_queue=AsyncMock(return_value=4),
            delete_pending_queue_scheduled_events=AsyncMock(return_value=0),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qdelete"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch(
        "src.handlers.claude.queue.TaskManager.cancel", new=AsyncMock(return_value=True)
    ) as mock_cancel:
        await handler(
            ack=AsyncMock(),
            command={
                "channel_id": "C123",
                "user_id": "U123",
                "text": "",
                "command": "/qdelete",
            },
            client=client,
            logger=MagicMock(),
        )

    deps.db.update_queue_control_state.assert_any_await("C123", None, "stopped")
    deps.db.update_queue_control_state.assert_any_await("C123", None, "running")
    deps.db.delete_queue.assert_awaited_once_with("C123", None)
    deps.db.delete_pending_queue_scheduled_events.assert_awaited_once_with("C123", None)
    mock_cancel.assert_awaited_once_with(_queue_task_id("C123", None))
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Deleted queue with 4 item(s)"


@pytest.mark.asyncio
async def test_qc_delete_deletes_entire_queue_scope():
    """`/qc delete` should remove all queue items and reset queue state."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            update_queue_control_state=AsyncMock(),
            delete_queue=AsyncMock(return_value=2),
            delete_pending_queue_scheduled_events=AsyncMock(return_value=0),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qc"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch(
        "src.handlers.claude.queue.TaskManager.cancel", new=AsyncMock(return_value=False)
    ) as mock_cancel:
        await handler(
            ack=AsyncMock(),
            command={
                "channel_id": "C123",
                "user_id": "U123",
                "text": "delete",
                "command": "/qc",
            },
            client=client,
            logger=MagicMock(),
        )

    deps.db.update_queue_control_state.assert_any_await("C123", None, "stopped")
    deps.db.update_queue_control_state.assert_any_await("C123", None, "running")
    deps.db.delete_queue.assert_awaited_once_with("C123", None)
    deps.db.delete_pending_queue_scheduled_events.assert_awaited_once_with("C123", None)
    mock_cancel.assert_awaited_once_with(_queue_task_id("C123", None))
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Deleted queue with 2 item(s)"


@pytest.mark.asyncio
async def test_q_parses_structured_plan_and_queues_all_items():
    """`/q` should parse structured plan markers and enqueue expanded prompts atomically."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(
                return_value=[
                    SimpleNamespace(id=1, position=1),
                    SimpleNamespace(id=2, position=2),
                    SimpleNamespace(id=3, position=3),
                ]
            ),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True):
        with patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="first",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                    SimpleNamespace(
                        prompt="second",
                        working_directory_override="/repo-worktrees/feature",
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                    SimpleNamespace(
                        prompt="third",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                ]
            ),
        ):
            with patch(
                "src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()
            ) as mock_ensure:
                await handler(
                    ack=AsyncMock(),
                    command={
                        "channel_id": "C123",
                        "user_id": "U123",
                        "text": "first\n***\nsecond",
                        "command": "/q",
                    },
                    client=client,
                    logger=MagicMock(),
                )

    deps.db.add_many_to_queue.assert_awaited_once_with(
        session_id=1,
        channel_id="C123",
        thread_ts=None,
        queue_entries=[
            ("first", None, None, None),
            ("second", "/repo-worktrees/feature", None, None),
            ("third", None, None, None),
        ],
        replace_pending=True,
    )
    assert "Queued 3 item(s) to queue" in client.chat_postMessage.await_args.kwargs["text"]
    mock_ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_q_add_structured_plan_can_append_with_explicit_directive():
    """Structured `/q` submissions can opt into append semantics."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=4, position=4)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True):
        with patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="next",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    )
                ]
            ),
        ):
            with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()):
                await handler(
                    ack=AsyncMock(),
                    command={
                        "channel_id": "C123",
                        "user_id": "U123",
                        "text": "***queue-append\nnext",
                        "command": "/q",
                    },
                    client=client,
                    logger=MagicMock(),
                )

    deps.db.add_many_to_queue.assert_awaited_once_with(
        session_id=1,
        channel_id="C123",
        thread_ts=None,
        queue_entries=[("next", None, None, None)],
        replace_pending=False,
    )
    assert "Added 1 item(s) to queue" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_q_add_structured_plan_defaults_to_append_when_queue_is_running():
    """Structured `/q` appends by default while an item is actively running."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=5, position=5)]),
            get_running_queue_items=AsyncMock(
                side_effect=[[SimpleNamespace(id=77)], [SimpleNamespace(id=77)]]
            ),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True):
        with patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="next",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    )
                ]
            ),
        ):
            with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()):
                await handler(
                    ack=AsyncMock(),
                    command={
                        "channel_id": "C123",
                        "user_id": "U123",
                        "text": "***loop-2\nnext",
                        "command": "/q",
                    },
                    client=client,
                    logger=MagicMock(),
                )

    deps.db.add_many_to_queue.assert_awaited_once_with(
        session_id=1,
        channel_id="C123",
        thread_ts=None,
        queue_entries=[("next", None, None, None)],
        replace_pending=False,
    )
    assert "Added 1 item(s) to queue" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_q_add_structured_plan_supports_clear_slash_directive():
    """Structured `/q` submissions accept `/clear` as replace-pending directive."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=4, position=4)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True):
        with patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="next",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    )
                ]
            ),
        ) as mock_materialize:
            with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()):
                await handler(
                    ack=AsyncMock(),
                    command={
                        "channel_id": "C123",
                        "user_id": "U123",
                        "text": "/clear\nnext",
                        "command": "/q",
                    },
                    client=client,
                    logger=MagicMock(),
                )

    deps.db.add_many_to_queue.assert_awaited_once_with(
        session_id=1,
        channel_id="C123",
        thread_ts=None,
        queue_entries=[("next", None, None, None)],
        replace_pending=True,
    )
    assert mock_materialize.await_args.kwargs["text"] == "next"


@pytest.mark.asyncio
async def test_q_add_structured_plan_persists_scheduled_controls():
    """Structured `/q` should persist schedule directives and start scheduler dispatcher."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=4, position=4)]),
            add_queue_scheduled_events=AsyncMock(return_value=[SimpleNamespace(id=501)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=30)
    with (
        patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True),
        patch(
            "src.handlers.claude.queue.parse_queue_plan_submission",
            return_value=(
                SimpleNamespace(
                    replace_pending=True,
                    directive_explicit=False,
                    scheduled_controls=[
                        SimpleNamespace(action="pause", execute_at=scheduled_time),
                    ],
                ),
                "next",
            ),
        ),
        patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="next",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    )
                ]
            ),
        ),
        patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()),
        patch(
            "src.handlers.claude.queue.ensure_queue_schedule_dispatcher",
            new=AsyncMock(),
        ) as mock_ensure_scheduler,
    ):
        await handler(
            ack=AsyncMock(),
            command={
                "channel_id": "C123",
                "user_id": "U123",
                "text": "***at 19:30 pause\nnext",
                "command": "/q",
            },
            client=client,
            logger=MagicMock(),
        )

    deps.db.add_queue_scheduled_events.assert_awaited_once_with(
        channel_id="C123",
        thread_ts=None,
        events=[("pause", scheduled_time)],
    )
    mock_ensure_scheduler.assert_awaited_once()
    assert "Scheduled controls:" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_q_add_does_not_restart_when_queue_is_paused():
    """`/q` should enqueue without auto-starting when the queue is paused."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=1, position=1)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control("paused")),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
        await handler(
            ack=AsyncMock(),
            command={
                "channel_id": "C123",
                "user_id": "U123",
                "text": "queued later",
                "command": "/q",
            },
            client=client,
            logger=MagicMock(),
        )

    mock_ensure.assert_not_awaited()
    assert "Queue is paused" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_q_structured_plan_restarts_when_replacing_paused_queue():
    """Structured `/q` replacements should start a fresh queue generation."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(return_value=[SimpleNamespace(id=1, position=1)]),
            get_running_queue_items=AsyncMock(return_value=[]),
            get_queue_control=AsyncMock(return_value=_queue_control("paused")),
            update_queue_control_state=AsyncMock(return_value=_queue_control("running")),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
        await handler(
            ack=AsyncMock(),
            command={
                "channel_id": "C123",
                "user_id": "U123",
                "text": "***loop-2\nqueued now",
                "command": "/q",
            },
            client=client,
            logger=MagicMock(),
        )

    deps.db.update_queue_control_state.assert_awaited_once_with("C123", None, "running")
    mock_ensure.assert_awaited_once()
    assert "Queue is paused" not in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_process_queue_uses_worktree_override_and_non_persistent_session_ids():
    """Worktree-scoped queue items should run with cwd override and in-memory resume IDs."""
    item1 = _queue_item(201, "task one", "/repo-worktrees/feature-x")
    item2 = _queue_item(202, "task two", "/repo-worktrees/feature-x")
    session = Session(id=1, working_directory="/repo", model="gpt-5.3-codex")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item1], [item2], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    call_sessions: list[Session] = []

    async def _fake_execute_for_session(**kwargs):
        call_sessions.append(kwargs["session"])
        if len(call_sessions) == 1:
            return SimpleNamespace(
                backend="codex",
                result=SimpleNamespace(
                    success=True,
                    output="done-one",
                    error=None,
                    session_id="codex-worktree-session-1",
                ),
            )
        return SimpleNamespace(
            backend="codex",
            result=SimpleNamespace(
                success=True,
                output="done-two",
                error=None,
                session_id="codex-worktree-session-2",
            ),
        )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=_fake_execute_for_session),
    ) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert mock_execute.await_count == 2
    first_kwargs = mock_execute.await_args_list[0].kwargs
    second_kwargs = mock_execute.await_args_list[1].kwargs
    assert first_kwargs["persist_session_ids"] is False
    assert second_kwargs["persist_session_ids"] is False
    assert call_sessions[0].working_directory == "/repo-worktrees/feature-x"
    assert call_sessions[0].codex_session_id is None
    assert call_sessions[1].working_directory == "/repo-worktrees/feature-x"
    assert call_sessions[1].codex_session_id == "codex-worktree-session-1"


@pytest.mark.asyncio
async def test_process_queue_parallel_group_honors_width_and_uses_isolated_scopes():
    """Parallel groups should respect width and use isolated execution scopes."""
    item1 = _queue_item(301, "task one")
    item1.parallel_group_id = "parallel-1"
    item1.parallel_limit = 2
    item2 = _queue_item(302, "task two")
    item2.parallel_group_id = "parallel-1"
    item2.parallel_limit = 2
    item3 = _queue_item(303, "task three")
    item3.parallel_group_id = "parallel-1"
    item3.parallel_limit = 2
    session = Session(
        id=1,
        working_directory="/repo",
        model="opus",
        claude_session_id="claude-main-session",
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item1, item2, item3], []]),
            get_queue_group_items=AsyncMock(return_value=[item1, item2, item3]),
            update_queue_item_status=AsyncMock(return_value=True),
            get_or_create_session=AsyncMock(return_value=session),
            get_command_history=AsyncMock(return_value=([], 0)),
            get_queue_control=AsyncMock(return_value=_queue_control()),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    active = 0
    max_active = 0
    call_sessions: list[Session] = []

    async def _fake_execute_for_session(**kwargs):
        nonlocal active, max_active
        call_sessions.append(kwargs["session"])
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(
            backend="claude",
            result=SimpleNamespace(success=True, output="done", error=None, session_id=None),
        )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=_fake_execute_for_session),
    ) as mock_execute:
        await _process_queue("C123", deps, client, MagicMock())

    assert max_active == 2
    assert mock_execute.await_count == 3
    for call in mock_execute.await_args_list:
        assert call.kwargs["persist_session_ids"] is False
        assert ":parallel:parallel-1:" in call.kwargs["session_scope_override"]
    assert all(session_arg.claude_session_id is None for session_arg in call_sessions)
