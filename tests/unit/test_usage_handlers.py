"""Unit tests for `/usage` command behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.claude.claude_cli import register_claude_cli_commands


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
async def test_usage_for_codex_session_uses_codex_status_snapshot():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo", codex_session_id="thread-1")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_claude_id=AsyncMock(),
            update_session_codex_id=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
            get_session_dirs=AsyncMock(return_value=[]),
            add_session_dir=AsyncMock(return_value=[]),
            remove_session_dir=AsyncMock(return_value=[]),
        ),
        codex_executor=SimpleNamespace(
            get_active_turn=AsyncMock(return_value={"turn_id": "turn-123"}),
            account_read=AsyncMock(
                return_value={
                    "account": {
                        "type": "chatgpt",
                        "email": "dev@example.com",
                        "planType": "pro",
                    }
                }
            ),
            config_read=AsyncMock(
                return_value={"config": {"model": "gpt-5.4", "model_reasoning_effort": "high"}}
            ),
            account_rate_limits_read=AsyncMock(
                return_value={
                    "rateLimitsByLimitId": {
                        "codex": {
                            "limitId": "codex",
                            "primary": {
                                "usedPercent": 1,
                                "windowDurationMins": 300,
                                "resetsAt": 1773291900,
                            },
                            "secondary": {
                                "usedPercent": 5,
                                "windowDurationMins": 10080,
                                "resetsAt": 1773852600,
                            },
                        }
                    }
                }
            ),
            thread_read=AsyncMock(return_value={"thread": {"id": "thread-1", "path": None}}),
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
        executor=SimpleNamespace(execute=AsyncMock()),
    )
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/usage"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/usage",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex usage"
    summary = kwargs["blocks"][0]["text"]["text"]
    assert "Codex Status" in summary
    assert "5h limit" in summary
    deps.executor.execute.assert_not_awaited()
    deps.codex_executor.account_rate_limits_read.assert_awaited_once_with("/repo")


@pytest.mark.asyncio
async def test_usage_for_claude_session_returns_app_native_claude_status():
    app = _FakeApp()
    session = Session(model="sonnet", working_directory="/repo", claude_session_id="claude-1")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_claude_id=AsyncMock(),
            update_session_codex_id=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
            get_session_dirs=AsyncMock(return_value=[]),
            add_session_dir=AsyncMock(return_value=[]),
            remove_session_dir=AsyncMock(return_value=[]),
        ),
        executor=SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    session_id="claude-2",
                    output="Cost output",
                    error=None,
                    detailed_output=None,
                    duration_ms=100,
                    cost_usd=0.01,
                    success=True,
                )
            ),
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
        codex_executor=SimpleNamespace(
            account_rate_limits_read=AsyncMock(),
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
    )
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/usage"]
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/usage",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.executor.execute.assert_not_awaited()
    deps.codex_executor.account_rate_limits_read.assert_not_awaited()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Claude usage"
    assert "Claude Session Status" in kwargs["blocks"][0]["text"]["text"]
