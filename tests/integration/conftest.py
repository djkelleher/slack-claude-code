"""Integration-test fixtures for slash command dispatch against a real Slack channel.

The ``slash_dispatch`` fixture initialises the app's handler layer with a temporary
SQLite database and stub executors, then wires it to the real Slack bot client so
that every slash command posts its output to the live test channel.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest_asyncio
from slack_sdk.web.async_client import AsyncWebClient

from src.database.migrations import init_database
from src.database.repository import DatabaseRepository
from src.handlers import register_commands
from src.handlers.base import HandlerDependencies
from tests.integration import helpers as _helpers

# ---------------------------------------------------------------------------
# Stub executor — satisfies cancel calls without a running backend
# ---------------------------------------------------------------------------


class _StubExecutor:
    """Minimal executor that satisfies handler cancel/interrupt calls."""

    async def cancel_by_scope(self, session_scope: str) -> int:
        return 0

    async def cancel_by_channel(self, channel_id: str) -> int:
        return 0

    async def interrupt_by_scope(self, session_scope: str) -> int:
        return 0

    async def interrupt_by_channel(self, channel_id: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Fake Bolt app — just collects registered handler functions
# ---------------------------------------------------------------------------


class _FakeApp:
    """Collects ``app.command()`` registrations so ``register_commands`` can run."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def command(self, name: str):
        """Decorator that records the handler function."""

        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator

    # Stubs for other registration hooks that may be called
    def action(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def view(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def event(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def options(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


# ---------------------------------------------------------------------------
# Dispatch helper
# ---------------------------------------------------------------------------


class SlashCommandDispatcher:
    """Invoke registered slash-command handlers with a real Slack client.

    This mirrors what Slack's infrastructure does: it calls the handler with
    ``ack``, ``command``, ``client``, and ``logger`` kwargs.  The handler then
    posts its response to the real Slack channel via the real client.
    """

    def __init__(
        self,
        handlers: dict[str, Any],
        client: AsyncWebClient,
        channel: str,
    ) -> None:
        self._handlers = handlers
        self._client = client
        self._channel = channel
        self._logger = logging.getLogger("live-test-slash-dispatch")

    @property
    def registered_commands(self) -> list[str]:
        return sorted(self._handlers)

    async def dispatch(
        self,
        command_name: str,
        text: str = "",
        thread_ts: str | None = None,
        user_id: str = "U_LIVE_TEST",
    ) -> None:
        """Invoke the handler for *command_name* as Slack would."""
        handler = self._handlers.get(command_name)
        if handler is None:
            raise ValueError(
                f"No handler registered for {command_name}. "
                f"Available: {', '.join(sorted(self._handlers))}"
            )

        async def _ack() -> None:
            return None

        command_payload: dict[str, Any] = {
            "channel_id": self._channel,
            "user_id": user_id,
            "text": text,
            "command": command_name,
        }
        if thread_ts:
            command_payload["thread_ts"] = thread_ts

        await handler(
            ack=_ack,
            command=command_payload,
            client=self._client,
            logger=self._logger,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Propagate --keep-messages flag to the helpers module."""
    _helpers.KEEP_MESSAGES = config.getoption("--keep-messages", default=False)


@pytest_asyncio.fixture(autouse=True)
async def _announce_test(request, slack_client, slack_test_channel):
    """Post a header message to Slack before each live test."""
    if "live" not in request.keywords:
        yield
        return

    test_name = request.node.name
    docstring = (request.function.__doc__ or "").strip().split("\n")[0]
    label = f":test_tube: *{test_name}*"
    if docstring:
        label += f"\n_{docstring}_"

    resp = await _helpers.slack_post_with_retry(
        slack_client,
        channel=slack_test_channel,
        text=label,
        blocks=[
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": label}],
            }
        ],
    )
    # Brief pause to avoid rate-limiting the handler's own API calls.
    await asyncio.sleep(0.5)
    yield
    await _helpers.delete_message(slack_client, slack_test_channel, resp["ts"])


@pytest_asyncio.fixture
async def slash_dispatch(
    slack_client: AsyncWebClient,
    slack_test_channel: str,
) -> SlashCommandDispatcher:
    """Set up the handler layer and return a dispatcher for slash commands.

    Uses a temporary SQLite database so tests don't pollute the production DB.
    The real Slack bot client is used so handler responses are posted to the
    live test channel.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_live.db")
        await init_database(db_path)
        db = DatabaseRepository(db_path)

        fake_app = _FakeApp()
        stub_executor = _StubExecutor()
        register_commands(
            app=fake_app,
            db=db,
            executor=stub_executor,
            codex_executor=stub_executor,
        )

        yield SlashCommandDispatcher(
            handlers=fake_app.handlers,
            client=slack_client,
            channel=slack_test_channel,
        )
