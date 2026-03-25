"""Shared model normalization and selection helpers for Slack handlers."""

import functools
from typing import Optional

from src.config import (
    CLAUDE_DEFAULT_ALIASES,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_MODEL_ALIASES,
    CLAUDE_MODEL_DISPLAY,
    CLAUDE_MODEL_OPTIONS,
    CODEX_BASE_MODEL_OPTIONS,
    CODEX_EFFORT_LABELS,
    CODEX_MODEL_ALIASES,
    CODEX_MODELS,
    EFFORT_LEVELS,
    get_backend_for_model,
    is_supported_codex_model,
    looks_like_codex_model,
    parse_claude_model_effort,
    parse_model_effort,
)

_EFFORT_ALIAS_MAP: dict[str, Optional[str]] = {
    "none": None,
    "default": None,
    "standard": None,
    "normal": None,
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "extrahigh": "xhigh",
}
_ALL_EFFORT_VALUES: set[str] = set(EFFORT_LEVELS) | set(CLAUDE_EFFORT_LEVELS)
_EFFORT_DISPLAY_LABELS: dict[Optional[str], str] = {
    None: "Standard",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra-High",
    "max": "Max",
    "auto": "Auto",
}


def normalize_model_name(model_name: str) -> Optional[str]:
    """Normalize model input into stored model identifier.

    Parameters
    ----------
    model_name : str
        User-supplied model alias or model identifier.

    Returns
    -------
    Optional[str]
        Canonical model ID for storage, or None for default model selection.
    """
    normalized = (model_name or "").strip().lower()
    if not normalized:
        return None

    codex_base_name, codex_effort = parse_model_effort(normalized)
    if codex_base_name in CODEX_MODEL_ALIASES or looks_like_codex_model(codex_base_name):
        resolved_codex_base = CODEX_MODEL_ALIASES.get(codex_base_name, codex_base_name)
        if codex_effort and looks_like_codex_model(resolved_codex_base):
            return f"{resolved_codex_base}-{codex_effort}"
        return resolved_codex_base

    claude_base_name, claude_effort = parse_claude_model_effort(normalized)
    resolved_claude_base = CLAUDE_MODEL_ALIASES.get(claude_base_name, claude_base_name)
    if resolved_claude_base is None:
        if claude_effort:
            # Default aliases should resolve to the current default Claude model ID
            # when users request an explicit effort suffix (e.g., "opus-high").
            if claude_base_name in CLAUDE_DEFAULT_ALIASES:
                return f"claude-opus-4-6-{claude_effort}"
            return f"{claude_base_name}-{claude_effort}"
        return None
    if claude_effort and claude_effort in CLAUDE_EFFORT_LEVELS:
        return f"{resolved_claude_base}-{claude_effort}"
    return resolved_claude_base


def normalize_effort_name(effort_name: Optional[str]) -> Optional[str]:
    """Normalize effort aliases to canonical values.

    Returns
    -------
    Optional[str]
        Canonical effort value, None for standard/no override, or raw normalized
        text when unknown.
    """
    normalized = (effort_name or "").strip().lower()
    if not normalized:
        return None
    if normalized in _EFFORT_ALIAS_MAP:
        return _EFFORT_ALIAS_MAP[normalized]
    if normalized in _ALL_EFFORT_VALUES:
        return normalized
    return normalized


def is_effort_token(token: Optional[str]) -> bool:
    """Return True when token is recognized as an effort argument."""
    normalized = (token or "").strip().lower()
    return normalized in _EFFORT_ALIAS_MAP or normalized in _ALL_EFFORT_VALUES


def split_model_input_and_effort(model_input: str) -> tuple[str, Optional[str]]:
    """Split raw `/model` text into model text + optional effort token."""
    normalized = (model_input or "").strip().lower()
    if not normalized:
        return "", None
    parts = normalized.split()
    if len(parts) < 2:
        return normalized, None
    effort_token = parts[-1]
    if not is_effort_token(effort_token):
        return normalized, None
    model_text = " ".join(parts[:-1]).strip()
    return model_text, normalize_effort_name(effort_token)


def split_model_and_effort(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split persisted model into base model + optional effort."""
    normalized_model = normalize_current_model(model)
    if normalized_model is None:
        return None, None

    codex_base, codex_effort = parse_model_effort(normalized_model)
    if looks_like_codex_model(codex_base):
        return codex_base.lower(), codex_effort

    claude_base, claude_effort = parse_claude_model_effort(normalized_model)
    if claude_effort and claude_effort in CLAUDE_EFFORT_LEVELS:
        return claude_base.lower(), claude_effort
    return normalized_model, None


def effort_display_name(effort: Optional[str]) -> str:
    """Return a display label for an effort value."""
    normalized = normalize_effort_name(effort)
    return _EFFORT_DISPLAY_LABELS.get(normalized, (normalized or "Standard").title())


def get_effort_options() -> list[dict[str, str]]:
    """Return effort picker options used by `/model` UI."""
    return [
        {
            "name": "standard",
            "value": "none",
            "display": "Standard",
            "desc": "No explicit effort override",
        },
        {
            "name": "low",
            "value": "low",
            "display": "Low",
            "desc": "Faster, lighter reasoning",
        },
        {
            "name": "medium",
            "value": "medium",
            "display": "Medium",
            "desc": "Balanced effort",
        },
        {
            "name": "high",
            "value": "high",
            "display": "High",
            "desc": "Deeper reasoning",
        },
        {
            "name": "xhigh",
            "value": "xhigh",
            "display": "Extra-High (Codex)",
            "desc": "Maximum Codex effort",
        },
        {
            "name": "max",
            "value": "max",
            "display": "Max (Claude)",
            "desc": "Maximum Claude effort",
        },
        {
            "name": "auto",
            "value": "auto",
            "display": "Auto (Claude)",
            "desc": "Let Claude choose effort",
        },
    ]


def apply_effort_to_model(
    model_value: Optional[str],
    effort_value: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Apply effort to a normalized model value.

    Returns
    -------
    tuple[Optional[str], Optional[str]]
        (effective_model, error_text). error_text is None when successful.
    """
    normalized_effort = normalize_effort_name(effort_value)
    if normalized_effort is None:
        return model_value, None

    if normalized_effort not in _ALL_EFFORT_VALUES:
        valid = ", ".join(
            [f"`{value}`" for value in ["low", "medium", "high", "xhigh", "max", "auto"]]
        )
        return model_value, f"Unsupported effort: `{effort_value}`. Valid efforts: {valid}."

    backend = get_backend_for_model(model_value)
    if backend == "codex":
        if normalized_effort not in EFFORT_LEVELS:
            return (
                model_value,
                "Codex effort must be one of: `low`, `medium`, `high`, `xhigh`.",
            )
        base_model, _ = parse_model_effort((model_value or "").lower())
        if not base_model:
            return model_value, "Cannot apply Codex effort without a model."
        return f"{base_model}-{normalized_effort}", None

    if normalized_effort not in CLAUDE_EFFORT_LEVELS:
        return (
            model_value,
            "Claude effort must be one of: `low`, `medium`, `high`, `max`, `auto`.",
        )
    if model_value is None:
        base_model = "claude-opus-4-6"
    else:
        base_model, _ = parse_claude_model_effort(model_value.lower())
        if base_model in CLAUDE_DEFAULT_ALIASES:
            base_model = "claude-opus-4-6"
    return f"{base_model}-{normalized_effort}", None


def get_claude_model_options() -> list[dict[str, str | None]]:
    """Return Claude model picker options."""
    return [option.__dict__.copy() for option in CLAUDE_MODEL_OPTIONS]


def get_codex_model_options() -> list[dict[str, str | None]]:
    """Return Codex model picker options."""
    return [option.__dict__.copy() for option in CODEX_BASE_MODEL_OPTIONS]


def get_all_model_options() -> list[dict[str, str | None]]:
    """Return combined Claude and Codex model picker options."""
    return get_claude_model_options() + get_codex_model_options()


@functools.lru_cache(maxsize=1)
def _model_selection_map() -> dict[str, tuple[Optional[str], str]]:
    """Build selection map from model button action key to value/display tuple."""
    mapping: dict[str, tuple[Optional[str], str]] = {}
    for option in get_all_model_options():
        option_name = option.get("name")
        option_value = option.get("value")
        option_display = option.get("display")
        if not option_name or not option_display:
            continue
        mapping[option_name] = (option_value, option_display)
    return mapping


def resolve_model_selection_action(model_name: str) -> tuple[Optional[str], str]:
    """Resolve a model-picker action key to normalized model value + display name."""
    normalized_name = (model_name or "").strip().lower()
    selection = _model_selection_map().get(normalized_name)
    if selection:
        return selection

    model_value = normalize_model_name(normalized_name)
    return model_value, model_display_name(model_value)


@functools.lru_cache(maxsize=1)
def _display_name_map() -> dict[Optional[str], str]:
    """Build model value -> display name lookup map."""
    mapping: dict[Optional[str], str] = dict(CLAUDE_MODEL_DISPLAY)
    for option in get_all_model_options():
        option_value = option.get("value")
        option_display = option.get("display")
        if not option_display:
            continue
        mapping[option_value] = option_display
    return mapping


def normalize_current_model(model: Optional[str]) -> Optional[str]:
    """Normalize persisted current model aliases for UI display."""
    if model is None:
        return None
    lowered = model.strip().lower()
    if lowered in CLAUDE_DEFAULT_ALIASES:
        return None
    return lowered


def model_display_name(model: Optional[str]) -> str:
    """Return human-readable display name for a model identifier."""
    normalized = normalize_current_model(model)
    display_map = _display_name_map()
    if normalized in display_map:
        return display_map[normalized]
    if normalized is None:
        return "Opus 4.6"

    codex_base, codex_effort = parse_model_effort(normalized)
    if codex_effort and codex_base:
        base_display = display_map.get(codex_base.lower(), codex_base.lower())
        effort_label = CODEX_EFFORT_LABELS.get(codex_effort, effort_display_name(codex_effort))
        return f"{base_display} ({effort_label})"

    claude_base, claude_effort = parse_claude_model_effort(normalized)
    if claude_effort and claude_base:
        base_display = display_map.get(claude_base.lower(), claude_base.lower())
        return f"{base_display} ({effort_display_name(claude_effort)})"

    return normalized


def codex_model_validation_error(model: Optional[str]) -> Optional[str]:
    """Return validation error text for unsupported Codex model IDs."""
    if not model:
        return None
    if not looks_like_codex_model(model):
        return None
    if is_supported_codex_model(model):
        return None

    supported = "\n".join(f"• `{entry}`" for entry in sorted(CODEX_MODELS))
    effort_levels = ", ".join(f"`{level}`" for level in EFFORT_LEVELS)
    return (
        f"Unsupported Codex model: `{model}`\n\n"
        f"Supported Codex models:\n{supported}\n\n"
        "Optional effort argument (space-separated): "
        f"`/model <model> <effort>` where effort is {effort_levels} or `extra-high`."
    )


def backend_label_for_model(model: Optional[str]) -> str:
    """Return user-facing backend label for the selected model."""
    backend = get_backend_for_model(model)
    return "Claude Code" if backend == "claude" else "OpenAI Codex"
