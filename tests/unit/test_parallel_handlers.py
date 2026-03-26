"""Unit tests for `/st` and `/cc` parallel command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.handlers.claude.parallel import register_parallel_commands
from src.utils.execution_scope import build_session_scope


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


@pytest.mark.asyncio
async def test_cc_cancels_channel_scoped_executions_when_not_in_thread() -> None:
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_active_jobs=AsyncMock(return_value=[SimpleNamespace(id=1), SimpleNamespace(id=2)]),
            cancel_job=AsyncMock(side_effect=[True, False]),
        ),
        executor=SimpleNamespace(
            cancel_by_scope=AsyncMock(return_value=99),
            cancel_by_channel=AsyncMock(return_value=3),
            cancel_all=AsyncMock(return_value=999),
        ),
    )
    register_parallel_commands(app, deps)

    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.handlers["/cc"]
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/cc",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.executor.cancel_by_channel.assert_awaited_once_with("C123")
    deps.executor.cancel_by_scope.assert_not_awaited()
    deps.executor.cancel_all.assert_not_awaited()
    assert (
        "Cancelled 1 job(s) and 3 active execution(s)."
        in client.chat_postMessage.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_cc_cancels_thread_scoped_executions_when_in_thread() -> None:
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_active_jobs=AsyncMock(return_value=[SimpleNamespace(id=1)]),
            cancel_job=AsyncMock(return_value=True),
        ),
        executor=SimpleNamespace(
            cancel_by_scope=AsyncMock(return_value=2),
            cancel_by_channel=AsyncMock(return_value=99),
            cancel_all=AsyncMock(return_value=999),
        ),
    )
    register_parallel_commands(app, deps)

    client = SimpleNamespace(chat_postMessage=AsyncMock())
    handler = app.handlers["/cc"]
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/cc",
            "thread_ts": "123.456",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.executor.cancel_by_scope.assert_awaited_once_with(build_session_scope("C123", "123.456"))
    deps.executor.cancel_by_channel.assert_not_awaited()
    deps.executor.cancel_all.assert_not_awaited()
    assert (
        "Cancelled 1 job(s) and 2 active execution(s)."
        in client.chat_postMessage.await_args.kwargs["text"]
    )
