"""Shared parsing and resolution for inline runtime mode directives."""

from dataclasses import dataclass
from typing import Optional

from src.codex.capabilities import (
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)
from src.config import config
from src.utils.model_selection import normalize_model_name

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


@dataclass(frozen=True)
class PlanModeDirective:
    """Structured plan-mode directive parsed from `(mode: ...)`."""

    strategy: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeDirectiveResolution:
    """Combined runtime directive resolution result."""

    overrides: RuntimeModeOverrides = RuntimeModeOverrides()
    plan_mode: Optional[PlanModeDirective] = None


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


def _resolve_single_mode_token(token: str, *, backend: str) -> RuntimeModeOverrides:
    """Resolve one mode token into runtime session overrides."""
    normalized = (token or "").strip().lower()
    if not normalized:
        raise ModeDirectiveError("Mode directive must include a mode value.")

    if normalized.startswith("approval "):
        raise ModeDirectiveError("Unsupported `approval` syntax. Use `approval: <mode>`.")
    if normalized.startswith("sandbox "):
        raise ModeDirectiveError("Unsupported `sandbox` syntax. Use `sandbox: <mode>`.")

    if normalized.startswith("approval:"):
        if backend != "codex":
            raise ModeDirectiveError(
                "`approval: ...` mode directives are only supported for Codex sessions."
            )
        approval_mode = normalized[len("approval:") :].strip()
        if approval_mode not in config.VALID_APPROVAL_MODES:
            valid = ", ".join(f"`{mode}`" for mode in config.VALID_APPROVAL_MODES)
            raise ModeDirectiveError(
                f"Invalid approval mode: `{approval_mode}`. Valid modes: {valid}."
            )
        return RuntimeModeOverrides(approval_mode=normalize_codex_approval_mode(approval_mode))

    if normalized.startswith("sandbox:"):
        if backend != "codex":
            raise ModeDirectiveError(
                "`sandbox: ...` mode directives are only supported for Codex sessions."
            )
        sandbox_mode = normalized[len("sandbox:") :].strip()
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


def _parse_plan_mode_token(token: str) -> Optional[PlanModeDirective]:
    """Parse one `splan`/`fplan` mode token."""
    normalized = (token or "").strip()
    lowered = normalized.lower()
    if not normalized:
        return None

    if lowered.startswith(("splan:", "fplan:")):
        raise ModeDirectiveError(
            "Unsupported plan strategy syntax. Use `splan <models>` or `fplan <models>`."
        )
    if lowered.startswith(("advs", "advf")):
        strategy = lowered.split(":", 1)[0].split(maxsplit=1)[0]
        if strategy in {"advs", "advf"}:
            raise ModeDirectiveError(f"`{strategy}` has been renamed. Use `splan`/`fplan` instead.")
    if lowered in {"splan", "fplan"}:
        raise ModeDirectiveError(f"`{lowered}` must include a comma-separated model list.")

    strategy: Optional[str] = None
    raw_values: Optional[str] = None
    for candidate in ("splan", "fplan"):
        prefix = f"{candidate} "
        if lowered.startswith(prefix):
            strategy = candidate
            raw_values = normalized[len(prefix) :].strip()
            break
    if strategy is None or raw_values is None:
        return None

    raw_models = [entry.strip() for entry in raw_values.split(",")]
    if any(not entry for entry in raw_models):
        raise ModeDirectiveError(
            f"`{strategy}` must provide a comma-separated list of model aliases."
        )
    if len(raw_models) < 2:
        raise ModeDirectiveError(
            f"`{strategy}` requires at least 2 models (planner + at least one reviewer)."
        )

    resolved_models: list[str] = []
    for model_alias in raw_models:
        normalized_model = normalize_model_name(model_alias)
        if normalized_model is None:
            raise ModeDirectiveError(
                f"Model alias `{model_alias}` resolves to default model; use an explicit model."
            )
        if not normalized_model.strip():
            raise ModeDirectiveError(f"Model alias `{model_alias}` is invalid.")
        resolved_models.append(normalized_model)

    return PlanModeDirective(strategy=strategy, models=tuple(resolved_models))


def resolve_runtime_mode_directives(mode_value: str, *, backend: str) -> RuntimeDirectiveResolution:
    """Resolve `(mode: ...)` content into runtime overrides + optional plan-mode strategy."""
    raw_value = (mode_value or "").strip()
    if not raw_value:
        raise ModeDirectiveError("Mode directive must include a mode value.")

    tokens = [token.strip() for token in raw_value.split(";") if token.strip()]
    if not tokens:
        raise ModeDirectiveError("Mode directive must include a mode value.")

    permission_mode: Optional[str] = None
    approval_mode: Optional[str] = None
    sandbox_mode: Optional[str] = None
    plan_mode: Optional[PlanModeDirective] = None

    for token in tokens:
        parsed_plan_mode = _parse_plan_mode_token(token)
        if parsed_plan_mode is not None:
            if plan_mode is not None:
                raise ModeDirectiveError(
                    "Only one plan strategy is allowed in `(mode: ...)`. Use either `splan` or `fplan`."
                )
            plan_mode = parsed_plan_mode
            continue

        if "," in token:
            raise ModeDirectiveError(
                "Commas are only supported inside `splan`/`fplan` model lists. "
                "Use semicolons to separate directives."
            )

        overrides = _resolve_single_mode_token(token, backend=backend)
        if overrides.permission_mode is not None:
            permission_mode = overrides.permission_mode
        if overrides.approval_mode is not None:
            approval_mode = overrides.approval_mode
        if overrides.sandbox_mode is not None:
            sandbox_mode = overrides.sandbox_mode

    return RuntimeDirectiveResolution(
        overrides=RuntimeModeOverrides(
            permission_mode=permission_mode,
            approval_mode=approval_mode,
            sandbox_mode=sandbox_mode,
        ),
        plan_mode=plan_mode,
    )


def resolve_runtime_mode_value(mode_value: str, *, backend: str) -> RuntimeModeOverrides:
    """Resolve `(mode: ...)` content into runtime session overrides."""
    return resolve_runtime_mode_directives(mode_value, backend=backend).overrides
