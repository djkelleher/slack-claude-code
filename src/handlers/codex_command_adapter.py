"""Codex-specific command adapter helpers used by slash-command handlers."""

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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


@dataclass(frozen=True)
class CodexRateLimitWindow:
    """Rate-limit window metadata."""

    used_percent: float
    window_minutes: Optional[int]
    resets_at: Optional[int]


@dataclass(frozen=True)
class CodexRateLimitSnapshot:
    """Normalized rate-limit snapshot payload."""

    limit_id: str
    limit_name: Optional[str]
    primary: Optional[CodexRateLimitWindow]
    secondary: Optional[CodexRateLimitWindow]


@dataclass(frozen=True)
class CodexTokenUsage:
    """Normalized context-usage summary."""

    total_tokens: Optional[int]
    context_window: Optional[int]


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


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    """Return the first present key value from a dictionary."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _to_int(value: Any) -> Optional[int]:
    """Convert numeric-like values to integer when possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    """Convert numeric-like values to float when possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_rate_window(raw_window: Any) -> Optional[CodexRateLimitWindow]:
    """Normalize a raw rate-limit window object."""
    if not isinstance(raw_window, dict):
        return None
    used_percent = _to_float(_first_value(raw_window, "usedPercent", "used_percent"))
    if used_percent is None:
        return None
    return CodexRateLimitWindow(
        used_percent=used_percent,
        window_minutes=_to_int(_first_value(raw_window, "windowDurationMins", "window_minutes")),
        resets_at=_to_int(_first_value(raw_window, "resetsAt", "resets_at")),
    )


def _normalize_rate_snapshot(
    raw_snapshot: Any, fallback_limit_id: str
) -> Optional[CodexRateLimitSnapshot]:
    """Normalize a raw rate-limit snapshot object."""
    if not isinstance(raw_snapshot, dict):
        return None
    limit_id = str(_first_value(raw_snapshot, "limitId", "limit_id") or fallback_limit_id).strip()
    limit_name_raw = _first_value(raw_snapshot, "limitName", "limit_name")
    limit_name = str(limit_name_raw).strip() if limit_name_raw else None
    primary = _normalize_rate_window(_first_value(raw_snapshot, "primary"))
    secondary = _normalize_rate_window(_first_value(raw_snapshot, "secondary"))
    if not primary and not secondary:
        return None
    return CodexRateLimitSnapshot(
        limit_id=limit_id or fallback_limit_id,
        limit_name=limit_name,
        primary=primary,
        secondary=secondary,
    )


def _extract_rate_limits_from_rpc(payload: dict[str, Any]) -> dict[str, CodexRateLimitSnapshot]:
    """Extract all known rate-limit snapshots from `account/rateLimits/read` payload."""
    snapshots: dict[str, CodexRateLimitSnapshot] = {}
    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        for key, value in by_limit_id.items():
            snapshot = _normalize_rate_snapshot(value, str(key))
            if snapshot:
                snapshots[snapshot.limit_id] = snapshot

    primary_snapshot = _normalize_rate_snapshot(payload.get("rateLimits"), "codex")
    if primary_snapshot and primary_snapshot.limit_id not in snapshots:
        snapshots[primary_snapshot.limit_id] = primary_snapshot
    return snapshots


def _extract_context_usage_from_info(raw_info: Any) -> Optional[CodexTokenUsage]:
    """Extract context window and total token usage from a raw token usage object."""
    if not isinstance(raw_info, dict):
        return None

    total_usage = _first_value(raw_info, "total_token_usage", "totalTokenUsage", "total")
    total_tokens = None
    if isinstance(total_usage, dict):
        total_tokens = _to_int(_first_value(total_usage, "total_tokens", "totalTokens"))

    context_window = _to_int(_first_value(raw_info, "model_context_window", "modelContextWindow"))
    if total_tokens is None and context_window is None:
        return None
    return CodexTokenUsage(total_tokens=total_tokens, context_window=context_window)


def _read_recent_session_usage(
    session_path: str,
    max_lines: int = 1500,
) -> tuple[Optional[CodexTokenUsage], dict[str, CodexRateLimitSnapshot]]:
    """Read recent `token_count` events from a Codex session log file."""
    path = Path(session_path).expanduser()
    if not path.exists() or not path.is_file():
        return None, {}

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            recent_lines = deque(handle, maxlen=max_lines)
    except OSError:
        return None, {}

    context_usage: Optional[CodexTokenUsage] = None
    snapshots: dict[str, CodexRateLimitSnapshot] = {}
    token_events_seen = 0

    for line in reversed(recent_lines):
        try:
            event = json.loads(line)
        except Exception:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "token_count":
            continue
        token_events_seen += 1
        if token_events_seen > 100:
            break

        if context_usage is None:
            context_usage = _extract_context_usage_from_info(payload.get("info"))

        snapshot = _normalize_rate_snapshot(
            _first_value(payload, "rate_limits", "rateLimits"),
            "codex",
        )
        if snapshot and snapshot.limit_id not in snapshots:
            snapshots[snapshot.limit_id] = snapshot
        if context_usage and len(snapshots) >= 2:
            break

    return context_usage, snapshots


def _format_token_count(value: int) -> str:
    """Format token count as human-readable compact value."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _format_context_usage(usage: Optional[CodexTokenUsage]) -> str:
    """Format context window usage text."""
    if usage is None or usage.context_window is None or usage.total_tokens is None:
        return "Unavailable"

    total_tokens = max(usage.total_tokens, 0)
    context_window = max(usage.context_window, 1)
    used_percent = (total_tokens / context_window) * 100.0
    left_percent = max(0.0, 100.0 - used_percent)
    return (
        f"{left_percent:.0f}% left "
        f"({_format_token_count(total_tokens)} used / {_format_token_count(context_window)})"
    )


def _format_reset_time(epoch_seconds: Optional[int]) -> str:
    """Format reset timestamp for Slack display."""
    if epoch_seconds is None:
        return "reset unknown"
    return datetime.fromtimestamp(epoch_seconds).strftime("resets %H:%M on %d %b")


def _format_bar(remaining_percent: float, width: int = 20) -> str:
    """Render a compact ASCII progress bar from remaining percentage."""
    bounded = max(0.0, min(remaining_percent, 100.0))
    filled = int(round((bounded / 100.0) * width))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _format_rate_window(window: Optional[CodexRateLimitWindow], fallback_label: str) -> str:
    """Format one rate-limit window line."""
    if window is None:
        return f"• {fallback_label}: unavailable"
    remaining = max(0.0, 100.0 - window.used_percent)
    percent_text = f"{remaining:.0f}%"
    return (
        f"• {fallback_label}: {_format_bar(remaining)} {percent_text} left "
        f"({_format_reset_time(window.resets_at)})"
    )


def _window_label(window: Optional[CodexRateLimitWindow], default_label: str) -> str:
    """Build a human label for a window duration."""
    if window is None or window.window_minutes is None:
        return default_label
    if window.window_minutes == 300:
        return "5h limit"
    if window.window_minutes == 10080:
        return "Weekly limit"
    return f"{window.window_minutes}m limit"


def _format_account_text(account_read_payload: dict[str, Any]) -> str:
    """Format account summary from `account/read` payload."""
    account = account_read_payload.get("account")
    if not isinstance(account, dict):
        return "Unavailable"

    account_type = str(account.get("type") or "unknown")
    email = str(account.get("email") or "").strip()
    plan = str(account.get("planType") or "").strip()
    plan_display = plan.title() if plan else "Unknown"
    if email:
        return f"{email} ({plan_display})"
    return f"{account_type} ({plan_display})"


def _format_permissions(sandbox_mode: str, approval_mode: str) -> str:
    """Format sandbox and approval settings for status display."""
    sandbox_labels = {
        "danger-full-access": "Full Access",
        "workspace-write": "Workspace Write",
        "read-only": "Read Only",
    }
    sandbox_display = sandbox_labels.get(sandbox_mode, sandbox_mode)
    return f"{sandbox_display} (approval: {approval_mode})"


def _order_rate_limits(
    rate_limits: dict[str, CodexRateLimitSnapshot],
) -> list[CodexRateLimitSnapshot]:
    """Order default codex limits first, then named limits."""
    values = list(rate_limits.values())

    def sort_key(snapshot: CodexRateLimitSnapshot) -> tuple[int, str]:
        is_default = 0 if snapshot.limit_id in {"codex", "default"} else 1
        name = snapshot.limit_name or snapshot.limit_id
        return is_default, name.lower()

    return sorted(values, key=sort_key)


def _format_rate_limits(rate_limits: dict[str, CodexRateLimitSnapshot]) -> str:
    """Format grouped rate-limit lines."""
    if not rate_limits:
        return "Rate limits unavailable"

    sections: list[str] = []
    for index, snapshot in enumerate(_order_rate_limits(rate_limits)):
        if index == 0 and snapshot.limit_id in {"codex", "default"}:
            title = "*Rate limits*"
        else:
            title_name = snapshot.limit_name or snapshot.limit_id
            title = f"*{title_name}*"
        primary_label = _window_label(snapshot.primary, "Primary limit")
        secondary_label = _window_label(snapshot.secondary, "Secondary limit")
        sections.append(
            "\n".join(
                [
                    title,
                    _format_rate_window(snapshot.primary, primary_label),
                    _format_rate_window(snapshot.secondary, secondary_label),
                ]
            )
        )
    return "\n\n".join(sections)


async def build_codex_status_summary(
    *,
    codex_executor: Any,
    session: Any,
    channel_id: str,
    thread_ts: Optional[str],
) -> str:
    """Build a Codex-native usage/status summary for `/usage`."""
    sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
    approval_mode = normalize_codex_approval_mode(
        session.approval_mode or config.CODEX_APPROVAL_MODE
    )
    collaboration_mode = session.permission_mode or "default"
    model = session.model or config.DEFAULT_MODEL or "(default)"
    reasoning_effort = ""
    account_text = "Unavailable"
    context_text = "Unavailable"
    session_id = session.codex_session_id or "none"
    active_turn_text = "none"
    rate_limits: dict[str, CodexRateLimitSnapshot] = {}
    session_path = ""

    if codex_executor:
        scope = build_session_scope(channel_id, thread_ts)
        active_turn = await codex_executor.get_active_turn(scope)
        if active_turn:
            active_turn_text = str(active_turn.get("turn_id", "unknown"))

        try:
            account_read = await codex_executor.account_read(session.working_directory)
            account_text = _format_account_text(account_read)
        except Exception:
            account_text = "Unavailable"

        try:
            config_read = await codex_executor.config_read(session.working_directory)
            config_payload = config_read.get("config")
            if isinstance(config_payload, dict):
                config_model = config_payload.get("model")
                if isinstance(config_model, str) and config_model.strip():
                    model = config_model.strip()
                config_effort = config_payload.get("model_reasoning_effort")
                if isinstance(config_effort, str) and config_effort.strip():
                    reasoning_effort = config_effort.strip()
        except Exception:
            pass

        try:
            rate_limit_payload = await codex_executor.account_rate_limits_read(
                session.working_directory
            )
            rate_limits = _extract_rate_limits_from_rpc(rate_limit_payload)
        except Exception:
            rate_limits = {}

        if session.codex_session_id:
            try:
                thread_read = await codex_executor.thread_read(
                    thread_id=session.codex_session_id,
                    working_directory=session.working_directory,
                    include_turns=False,
                )
                thread = thread_read.get("thread")
                if isinstance(thread, dict):
                    thread_id = thread.get("id")
                    if isinstance(thread_id, str) and thread_id.strip():
                        session_id = thread_id
                    path_value = thread.get("path")
                    if isinstance(path_value, str) and path_value.strip():
                        session_path = path_value
            except Exception:
                session_path = ""

    if session_path:
        usage_from_log, log_rate_limits = _read_recent_session_usage(session_path)
        context_text = _format_context_usage(usage_from_log)
        if not rate_limits and log_rate_limits:
            rate_limits = log_rate_limits

    model_text = model
    if reasoning_effort:
        model_text = f"{model} (reasoning {reasoning_effort})"

    agents_path = Path(session.working_directory).expanduser() / "AGENTS.md"
    agents_text = "AGENTS.md" if agents_path.exists() else "Not found"
    session_display = f"`{session_id}`" if session_id != "none" else "_No active session_"

    return (
        "*Codex Status*\n"
        f"*Model:* `{model_text}`\n"
        f"*Directory:* `{session.working_directory}`\n"
        f"*Permissions:* {_format_permissions(sandbox_mode, approval_mode)}\n"
        f"*Agents.md:* {agents_text}\n"
        f"*Account:* {account_text}\n"
        f"*Collaboration mode:* `{collaboration_mode}`\n"
        f"*Session:* {session_display}\n"
        f"*Active turn:* `{active_turn_text}`\n\n"
        f"*Context window:* {context_text}\n\n"
        f"{_format_rate_limits(rate_limits)}"
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
