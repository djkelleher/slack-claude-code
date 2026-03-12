"""Helpers for routing slash-command text through registered handler functions."""

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class ParsedSlashCommand:
    """Normalized slash-command text payload."""

    name: str
    text: str


def parse_slash_command_text(raw_text: str) -> Optional[ParsedSlashCommand]:
    """Parse slash-command text into command name and trailing argument text."""
    stripped = (raw_text or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    name = parts[0].strip()
    if not name:
        return None
    text = parts[1].strip() if len(parts) > 1 else ""
    return ParsedSlashCommand(name=name, text=text)


class SlashCommandRouter:
    """Dispatch slash-command text through already-registered Slack handlers."""

    def __init__(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self._handlers = dict(handlers)

    def has_command(self, command_name: str) -> bool:
        """Return True when this router has a matching slash-command handler."""
        return command_name in self._handlers

    async def dispatch(
        self,
        *,
        command_name: str,
        command_text: str,
        channel_id: str,
        thread_ts: Optional[str],
        user_id: str,
        client: Any,
        logger: Any,
    ) -> bool:
        """Dispatch command payload to the registered slash-command handler."""
        if command_name not in self._handlers:
            return False

        handler = self._handlers[command_name]

        async def _ack() -> None:
            return None

        command_payload = {
            "channel_id": channel_id,
            "user_id": user_id,
            "text": command_text,
            "command": command_name,
        }
        if thread_ts:
            command_payload["thread_ts"] = thread_ts

        await handler(
            ack=_ack,
            command=command_payload,
            client=client,
            logger=logger,
        )
        return True


def _extract_command_name_from_listener(listener: Any) -> Optional[str]:
    """Extract literal slash command name from a Bolt async listener matcher closure."""
    try:
        matcher = listener.matchers[0]
        matcher_func = matcher.func
    except (AttributeError, IndexError):
        return None

    closure = matcher_func.__closure__
    if not closure:
        return None

    matcher_impl = closure[0].cell_contents
    inner_closure = matcher_impl.__closure__
    if not inner_closure:
        return None

    command_value = inner_closure[0].cell_contents
    if isinstance(command_value, str) and command_value.startswith("/"):
        return command_value
    return None


def build_slash_command_router(app: Any) -> SlashCommandRouter:
    """Build slash-command router from a Slack Bolt app or test double."""
    handlers: dict[str, Callable[..., Any]] = {}

    try:
        fake_handlers = app.handlers
    except AttributeError:
        fake_handlers = None
    if isinstance(fake_handlers, dict):
        for command_name, handler in fake_handlers.items():
            if isinstance(command_name, str) and command_name.startswith("/"):
                handlers[command_name] = handler

    try:
        listeners = app._async_listeners
    except AttributeError:
        listeners = []
    for listener in listeners:
        command_name = _extract_command_name_from_listener(listener)
        if not command_name:
            continue
        if command_name in handlers:
            continue
        try:
            handlers[command_name] = listener.ack_function
        except AttributeError:
            continue

    return SlashCommandRouter(handlers)
