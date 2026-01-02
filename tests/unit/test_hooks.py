"""Unit tests for the hooks system."""

import asyncio
import pytest

from src.hooks import HookRegistry, HookEvent, HookEventType, HookContext, HookResult, hook, create_context


class TestHookRegistry:
    """Tests for HookRegistry class."""

    @pytest.mark.asyncio
    async def test_register_and_emit(self):
        """Basic handler registration and emission works."""
        results = []

        async def handler(event: HookEvent):
            results.append(event.data.get("value"))
            return "handled"

        HookRegistry.register(HookEventType.RESULT, handler, name="test_handler")

        ctx = create_context(session_id="test-session")
        event = HookEvent(
            event_type=HookEventType.RESULT,
            context=ctx,
            data={"value": 42},
        )

        hook_results = await HookRegistry.emit(event)

        assert len(results) == 1
        assert results[0] == 42
        assert len(hook_results) == 1
        assert hook_results[0].success is True
        assert hook_results[0].handler_name == "test_handler"
        assert hook_results[0].result == "handled"

    @pytest.mark.asyncio
    async def test_handler_error_isolation(self):
        """One handler failure doesn't affect others."""
        results = []

        async def failing_handler(event: HookEvent):
            raise ValueError("Intentional error")

        async def succeeding_handler(event: HookEvent):
            results.append("success")
            return "ok"

        HookRegistry.register(HookEventType.ERROR, failing_handler, name="failing")
        HookRegistry.register(HookEventType.ERROR, succeeding_handler, name="succeeding")

        ctx = create_context(session_id="test-session")
        event = HookEvent(
            event_type=HookEventType.ERROR,
            context=ctx,
            data={},
        )

        hook_results = await HookRegistry.emit(event)

        # Both handlers were called
        assert len(hook_results) == 2

        # Succeeding handler still ran
        assert len(results) == 1
        assert results[0] == "success"

        # Results indicate which succeeded/failed
        result_by_name = {r.handler_name: r for r in hook_results}
        assert result_by_name["failing"].success is False
        assert "Intentional error" in result_by_name["failing"].error
        assert result_by_name["succeeding"].success is True

    @pytest.mark.asyncio
    async def test_concurrent_handlers(self):
        """Handlers run concurrently."""
        execution_order = []
        start_order = []

        async def slow_handler(event: HookEvent):
            start_order.append("slow")
            await asyncio.sleep(0.1)
            execution_order.append("slow")

        async def fast_handler(event: HookEvent):
            start_order.append("fast")
            execution_order.append("fast")

        HookRegistry.register(HookEventType.SESSION_START, slow_handler, name="slow")
        HookRegistry.register(HookEventType.SESSION_START, fast_handler, name="fast")

        ctx = create_context(session_id="test-session")
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            context=ctx,
            data={},
        )

        await HookRegistry.emit(event)

        # Both started (order may vary due to concurrency)
        assert len(start_order) == 2
        # Fast finishes before slow due to concurrent execution
        assert execution_order == ["fast", "slow"]

    @pytest.mark.asyncio
    async def test_hook_decorator(self):
        """@hook decorator registers handlers."""
        results = []

        @hook(HookEventType.TOOL_USE, name="decorator_handler")
        async def decorated_handler(event: HookEvent):
            results.append(event.data.get("tool"))
            return "decorated"

        # Handler should be registered automatically
        handlers = HookRegistry.list_handlers(HookEventType.TOOL_USE)
        assert "decorator_handler" in handlers[HookEventType.TOOL_USE.value]

        ctx = create_context(session_id="test-session")
        event = HookEvent(
            event_type=HookEventType.TOOL_USE,
            context=ctx,
            data={"tool": "Bash"},
        )

        await HookRegistry.emit(event)
        assert results == ["Bash"]

    def test_unregister_by_name(self):
        """Handler removal by name works."""

        async def handler(event: HookEvent):
            pass

        HookRegistry.register(HookEventType.NOTIFICATION, handler, name="removable")

        # Verify registered
        handlers = HookRegistry.list_handlers(HookEventType.NOTIFICATION)
        assert "removable" in handlers[HookEventType.NOTIFICATION.value]

        # Unregister by name
        removed = HookRegistry.unregister(HookEventType.NOTIFICATION, name="removable")
        assert removed is True

        # Verify removed
        handlers = HookRegistry.list_handlers(HookEventType.NOTIFICATION)
        assert "removable" not in handlers.get(HookEventType.NOTIFICATION.value, [])

    def test_unregister_by_handler_function(self):
        """Handler removal by function reference works."""

        async def my_handler(event: HookEvent):
            pass

        HookRegistry.register(HookEventType.COST_UPDATE, my_handler)

        # Unregister by function reference
        removed = HookRegistry.unregister(HookEventType.COST_UPDATE, handler=my_handler)
        assert removed is True

    def test_unregister_nonexistent_returns_false(self):
        """Unregistering non-existent handler returns False."""
        removed = HookRegistry.unregister(HookEventType.SESSION_END, name="nonexistent")
        assert removed is False

    @pytest.mark.asyncio
    async def test_emit_no_handlers(self):
        """Emitting with no registered handlers returns empty list."""
        ctx = create_context(session_id="test-session")
        event = HookEvent(
            event_type=HookEventType.SESSION_END,  # No handlers registered
            context=ctx,
            data={},
        )

        results = await HookRegistry.emit(event)
        assert results == []

    def test_list_handlers_all(self):
        """list_handlers() returns all registered handlers."""

        async def h1(event: HookEvent):
            pass

        async def h2(event: HookEvent):
            pass

        HookRegistry.register(HookEventType.RESULT, h1, name="h1")
        HookRegistry.register(HookEventType.ERROR, h2, name="h2")

        all_handlers = HookRegistry.list_handlers()
        assert HookEventType.RESULT.value in all_handlers
        assert HookEventType.ERROR.value in all_handlers
        assert "h1" in all_handlers[HookEventType.RESULT.value]
        assert "h2" in all_handlers[HookEventType.ERROR.value]

    def test_clear_specific_event_type(self):
        """clear() with event_type only clears that type."""

        async def h1(event: HookEvent):
            pass

        async def h2(event: HookEvent):
            pass

        HookRegistry.register(HookEventType.RESULT, h1, name="h1")
        HookRegistry.register(HookEventType.ERROR, h2, name="h2")

        HookRegistry.clear(HookEventType.RESULT)

        handlers = HookRegistry.list_handlers()
        assert HookEventType.RESULT.value not in handlers or handlers[HookEventType.RESULT.value] == []
        assert "h2" in handlers.get(HookEventType.ERROR.value, [])


class TestCreateContext:
    """Tests for create_context helper."""

    def test_create_context_minimal(self):
        """create_context with just session_id works."""
        ctx = create_context(session_id="my-session")

        assert ctx.session_id == "my-session"
        assert ctx.channel_id == "my-session"  # Defaults to session_id
        assert ctx.thread_ts is None
        assert ctx.user_id is None
        assert ctx.working_directory is None

    def test_create_context_full(self):
        """create_context with all parameters works."""
        ctx = create_context(
            session_id="session-123",
            channel_id="C123ABC",
            thread_ts="1234567890.123456",
            user_id="U123ABC",
            working_directory="/home/user/project",
        )

        assert ctx.session_id == "session-123"
        assert ctx.channel_id == "C123ABC"
        assert ctx.thread_ts == "1234567890.123456"
        assert ctx.user_id == "U123ABC"
        assert ctx.working_directory == "/home/user/project"


class TestHookEvent:
    """Tests for HookEvent dataclass."""

    def test_event_properties(self):
        """HookEvent provides convenience properties."""
        ctx = HookContext(
            session_id="sess-1",
            channel_id="chan-1",
        )
        event = HookEvent(
            event_type=HookEventType.RESULT,
            context=ctx,
            data={"key": "value"},
        )

        assert event.session_id == "sess-1"
        assert event.channel_id == "chan-1"
        assert event.data["key"] == "value"
        assert event.timestamp is not None

    def test_event_type_values(self):
        """All HookEventType values are valid strings."""
        expected_types = [
            "session_start",
            "session_end",
            "tool_use",
            "tool_result",
            "approval_needed",
            "approval_response",
            "result",
            "error",
            "cost_update",
            "notification",
        ]

        actual_types = [e.value for e in HookEventType]
        assert set(actual_types) == set(expected_types)


class TestHookResult:
    """Tests for HookResult dataclass."""

    def test_successful_result(self):
        """HookResult for successful execution."""
        result = HookResult(
            success=True,
            handler_name="my_handler",
            result={"data": 123},
            duration_ms=50,
        )

        assert result.success is True
        assert result.handler_name == "my_handler"
        assert result.result == {"data": 123}
        assert result.error is None
        assert result.duration_ms == 50

    def test_failed_result(self):
        """HookResult for failed execution."""
        result = HookResult(
            success=False,
            handler_name="failing_handler",
            error="Connection timeout",
            duration_ms=1000,
        )

        assert result.success is False
        assert result.error == "Connection timeout"
        assert result.result is None
