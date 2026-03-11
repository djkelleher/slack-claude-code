"""Backend command adapter helpers shared by slash-command handlers."""

from dataclasses import dataclass
from typing import Any, Optional

from src.codex.capabilities import (
    get_codex_hint_for_claude_command,
    is_claude_only_slash_command,
    normalize_codex_approval_mode,
)
from src.config import config
from src.utils.execution_scope import build_session_scope


@dataclass(frozen=True)
class CodexUsageSnapshot:
    """Resolved Codex session status values for `/usage` rendering."""

    sandbox_mode: str
    approval_mode: str
    model: str
    has_session: str
    active_turn_text: str
    models_text: str
    account_text: str
    mcp_text: str
    features_text: str


def unsupported_claude_slash_command_message(session: Any, command_name: str) -> Optional[str]:
    """Return Codex guidance when a Claude-only slash command is used."""
    if session.get_backend() != "codex":
        return None
    if not is_claude_only_slash_command(command_name):
        return None
    return get_codex_hint_for_claude_command(command_name)


async def get_codex_usage_snapshot(
    *,
    codex_executor: Any,
    session: Any,
    channel_id: str,
    thread_ts: Optional[str],
) -> CodexUsageSnapshot:
    """Collect best-effort Codex usage details for `/usage` command output."""
    sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
    approval_mode = normalize_codex_approval_mode(
        session.approval_mode or config.CODEX_APPROVAL_MODE
    )
    model = session.model or config.DEFAULT_MODEL or "(default)"
    has_session = ":white_check_mark:" if session.codex_session_id else ":x:"
    active_turn_text = ":x:"
    models_text = "n/a"
    account_text = "n/a"
    mcp_text = "n/a"
    features_text = "n/a"

    if codex_executor:
        scope = build_session_scope(channel_id, thread_ts)
        active_turn = await codex_executor.get_active_turn(scope)
        if active_turn:
            turn_id = active_turn.get("turn_id", "unknown")
            active_turn_text = f":white_check_mark: `{turn_id}`"

        try:
            model_list = await codex_executor.model_list(session.working_directory)
            models_text = str(len(model_list.get("data", [])))
        except Exception:
            models_text = "unavailable"

        try:
            account_read = await codex_executor.account_read(session.working_directory)
            account = account_read.get("account")
            if isinstance(account, dict):
                account_type = account.get("type", "unknown")
                if account_type == "chatgpt":
                    account_text = (
                        f"{account_type} ({account.get('planType', 'unknown')}) "
                        f"{account.get('email', '')}".strip()
                    )
                else:
                    account_text = account_type
            else:
                account_text = "none"
        except Exception:
            account_text = "unavailable"

        try:
            mcp_status = await codex_executor.mcp_server_status_list(session.working_directory)
            mcp_text = str(len(mcp_status.get("data", [])))
        except Exception:
            mcp_text = "unavailable"

        try:
            features = await codex_executor.experimental_feature_list(session.working_directory)
            features_text = str(len(features.get("data", [])))
        except Exception:
            features_text = "unavailable"

    return CodexUsageSnapshot(
        sandbox_mode=sandbox_mode,
        approval_mode=approval_mode,
        model=model,
        has_session=has_session,
        active_turn_text=active_turn_text,
        models_text=models_text,
        account_text=account_text,
        mcp_text=mcp_text,
        features_text=features_text,
    )


async def get_codex_mcp_summary(codex_executor: Any, working_directory: str) -> str:
    """Return markdown summary for Codex MCP server status."""
    status = await codex_executor.mcp_server_status_list(working_directory)
    servers = status.get("data", [])
    if not servers:
        return "No MCP servers detected."

    lines = []
    for server in servers[:10]:
        name = server.get("name", "unknown")
        auth_status = server.get("authStatus", "unknown")
        tools = server.get("tools", {})
        resources = server.get("resources", [])
        lines.append(
            f"• *{name}*\nauth: `{auth_status}` • tools: `{len(tools)}` • resources: `{len(resources)}`"
        )
    return "*Codex MCP Servers*\n" + "\n\n".join(lines)


def format_codex_review_status(thread: dict, fallback_thread_id: str) -> str:
    """Format thread-read payload into Slack markdown review summary."""
    turns = thread.get("turns", [])
    if turns:
        recent_turns = turns[-5:]
        turn_lines = []
        for turn in recent_turns:
            turn_lines.append(
                f"• `{turn.get('id', 'unknown')}` status=`{turn.get('status', 'unknown')}` "
                f"created=`{turn.get('createdAt', 'n/a')}`"
            )
        turns_text = "\n".join(turn_lines)
        latest_status = recent_turns[-1].get("status", thread.get("status", "unknown"))
    else:
        turns_text = "No turns found."
        latest_status = thread.get("status", "unknown")

    return (
        f"*Codex Review Status*\n"
        f"Thread: `{thread.get('id', fallback_thread_id)}`\n"
        f"Name: {thread.get('name') or '(unnamed)'}\n"
        f"Status: `{latest_status}`\n"
        f"Turns: `{len(turns)}`\n\n"
        f"*Recent Turns*\n{turns_text}"
    )
