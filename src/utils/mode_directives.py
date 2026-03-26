"""Shared parsing and resolution for inline runtime mode directives."""

from dataclasses import dataclass
from typing import Optional

from src.codex.capabilities import (
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)
from src.config import config

CLAUDE_MODE_ALIASES: dict[str, str] = {
    "bypass": config.DEFAULT_BYPASS_MODE,
    "accept": "acceptEdits",
    "default": "default",
    "plan": "plan",
    "ask": "default",
    "delegate": "delegate",
}


class ModeDirectiveError(ValueError):
    """Raised when a runtime mode directive is malformed or unsupported."""


@dataclass(frozen=True)
class RuntimeModeOverrides:
    """Ephemeral per-execution mode overrides."""

    permission_mode: Optional[str] = None
    approval_mode: Optional[str] = None
    sandbox_mode: Optional[str] = None


def map_codex_alias_to_permission_mode(alias: str) -> str:
    """Map Codex compatibility alias to stored permission mode."""
    normalized = (alias or "").strip().lower()
    if normalized == "bypass":
        return config.DEFAULT_BYPASS_MODE
    if normalized == "plan":
        return "plan"
    return "default"


def parse_parenthesized_mode_directive_line(line: str) -> Optional[str]:
    """Parse one parenthesized mode directive line and return its value."""
    stripped = (line or "").strip()
    if not stripped:
        return None

    if stripped.startswith("((") and stripped.endswith("))"):
        body = stripped[2:-2].strip()
    elif stripped.startswith("(") and stripped.endswith(")"):
        body = stripped[1:-1].strip()
    else:
        return None

    if "," in body:
        return None
    if ":" not in body:
        return None

    key, value = body.split(":", 1)
    if key.strip().lower() != "mode":
        return None

    mode_value = value.strip()
    if not mode_value:
        raise ModeDirectiveError("Mode directive must include a mode value.")
    return mode_value


def is_parenthesized_end_marker(line: str) -> bool:
    """Return True when line is a standalone `(end)` / `((end))` marker."""
    stripped = (line or "").strip().lower()
    return stripped in {"(end)", "((end))"}


def resolve_runtime_mode_value(mode_value: str, *, backend: str) -> RuntimeModeOverrides:
    """Resolve `(mode: ...)` content into runtime session overrides."""
    normalized = (mode_value or "").strip().lower()
    if not normalized:
        raise ModeDirectiveError("Mode directive must include a mode value.")

    if normalized.startswith("approval "):
        if backend != "codex":
            raise ModeDirectiveError(
                "`approval ...` mode directives are only supported for Codex sessions."
            )
        approval_mode = normalized[len("approval ") :].strip()
        if approval_mode not in config.VALID_APPROVAL_MODES:
            valid = ", ".join(f"`{mode}`" for mode in config.VALID_APPROVAL_MODES)
            raise ModeDirectiveError(
                f"Invalid approval mode: `{approval_mode}`. Valid modes: {valid}."
            )
        return RuntimeModeOverrides(approval_mode=normalize_codex_approval_mode(approval_mode))

    if normalized.startswith("sandbox "):
        if backend != "codex":
            raise ModeDirectiveError(
                "`sandbox ...` mode directives are only supported for Codex sessions."
            )
        sandbox_mode = normalized[len("sandbox ") :].strip()
        if sandbox_mode not in config.VALID_SANDBOX_MODES:
            valid = ", ".join(f"`{mode}`" for mode in config.VALID_SANDBOX_MODES)
            raise ModeDirectiveError(
                f"Invalid sandbox mode: `{sandbox_mode}`. Valid modes: {valid}."
            )
        return RuntimeModeOverrides(sandbox_mode=sandbox_mode)

    if backend == "codex":
        resolved = resolve_codex_compat_mode(normalized)
        if resolved.error:
            raise ModeDirectiveError(resolved.error)
        return RuntimeModeOverrides(
            permission_mode=map_codex_alias_to_permission_mode(normalized),
            approval_mode=resolved.approval_mode,
        )

    permission_mode = CLAUDE_MODE_ALIASES.get(normalized)
    if permission_mode is None:
        valid_aliases = ", ".join(f"`{name}`" for name in sorted(CLAUDE_MODE_ALIASES))
        raise ModeDirectiveError(f"Unknown mode: `{normalized}`. Valid aliases: {valid_aliases}.")
    return RuntimeModeOverrides(permission_mode=permission_mode)
