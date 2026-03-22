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
            return f"{claude_base_name}-{claude_effort}"
        return None
    if claude_effort and claude_effort in CLAUDE_EFFORT_LEVELS:
        return f"{resolved_claude_base}-{claude_effort}"
    return resolved_claude_base


def get_claude_model_options() -> list[dict[str, str | None]]:
    """Return Claude model picker options."""
    return [option.__dict__.copy() for option in CLAUDE_MODEL_OPTIONS]


def get_codex_model_options() -> list[dict[str, str | None]]:
    """Return Codex model picker options including effort variants."""
    base_models = [option.__dict__.copy() for option in CODEX_BASE_MODEL_OPTIONS]
    effort_variants: list[dict[str, str | None]] = []
    for model in base_models:
        model_name = model["name"]
        model_value = model["value"]
        model_display = model["display"]
        model_desc = model["desc"]
        if not model_name or not model_value or not model_display:
            continue
        for effort_key, effort_label in CODEX_EFFORT_LABELS.items():
            effort_variants.append(
                {
                    "name": f"{model_name}-{effort_key}",
                    "value": f"{model_value}-{effort_key}",
                    "display": f"{model_display} ({effort_label})",
                    "desc": model_desc,
                }
            )
    return base_models + effort_variants


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
    return _display_name_map().get(normalized, normalized or "Default (recommended)")


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
        f"Optional effort suffixes: {effort_levels}, `extra-high`"
    )


def backend_label_for_model(model: Optional[str]) -> str:
    """Return user-facing backend label for the selected model."""
    backend = get_backend_for_model(model)
    return "Claude Code" if backend == "claude" else "OpenAI Codex"
