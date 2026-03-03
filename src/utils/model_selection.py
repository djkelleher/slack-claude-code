"""Shared model normalization and selection helpers for Slack handlers."""

import functools
from typing import Optional

from src.config import (
    CODEX_MODELS,
    EFFORT_LEVELS,
    get_backend_for_model,
    is_supported_codex_model,
    looks_like_codex_model,
    parse_model_effort,
)

CLAUDE_MODEL_DISPLAY: dict[str | None, str] = {
    None: "Default (recommended)",
    "default": "Default (recommended)",
    "opus": "Default (recommended)",
    "claude-opus-4-6": "Default (recommended)",
    "claude-opus-4-6[1m]": "Opus (1M context)",
    "sonnet": "Sonnet",
    "claude-sonnet-4-6": "Sonnet",
    "claude-sonnet-4-6[1m]": "Sonnet (1M context)",
    "haiku": "Haiku",
    "claude-haiku-4-5": "Haiku",
}

_CLAUDE_MODEL_ALIASES: dict[str, str | None] = {
    "default": None,
    "default (recommended)": None,
    "recommended": None,
    "opus": None,
    "opus-4.6": None,
    "claude-opus-4-6": None,
    "opus-1m": "claude-opus-4-6[1m]",
    "opus (1m context)": "claude-opus-4-6[1m]",
    "claude-opus-4-6[1m]": "claude-opus-4-6[1m]",
    "sonnet": "sonnet",
    "sonnet-4.6": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "sonnet-1m": "claude-sonnet-4-6[1m]",
    "sonnet (1m context)": "claude-sonnet-4-6[1m]",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6[1m]",
    "haiku": "haiku",
    "haiku-4.5": "haiku",
    "claude-haiku-4-5": "haiku",
}

_CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex": "gpt-5.3-codex",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
}

_CLAUDE_DEFAULT_ALIASES: set[str] = {"default", "opus", "claude-opus-4-6"}

_CLAUDE_MODEL_OPTIONS: tuple[dict[str, str | None], ...] = (
    {
        "name": "default",
        "value": None,
        "display": "Default (recommended)",
        "desc": "Opus 4.6 · Most capable for complex work",
    },
    {
        "name": "opus-1m",
        "value": "claude-opus-4-6[1m]",
        "display": "Opus (1M context)",
        "desc": "Opus 4.6 with 1M context · Billed as extra usage · $10/$37.50 per Mtok",
    },
    {
        "name": "sonnet",
        "value": "sonnet",
        "display": "Sonnet",
        "desc": "Sonnet 4.6 · Best for everyday tasks",
    },
    {
        "name": "sonnet-1m",
        "value": "claude-sonnet-4-6[1m]",
        "display": "Sonnet (1M context)",
        "desc": "Sonnet 4.6 with 1M context · Billed as extra usage · $6/$22.50 per Mtok",
    },
    {
        "name": "haiku",
        "value": "haiku",
        "display": "Haiku",
        "desc": "Haiku 4.5 · Fastest for quick answers",
    },
)

_CODEX_BASE_MODEL_OPTIONS: tuple[dict[str, str | None], ...] = (
    {
        "name": "gpt-5.3-codex",
        "value": "gpt-5.3-codex",
        "display": "GPT-5.3 Codex",
        "desc": "Latest frontier agentic coding model",
    },
    {
        "name": "gpt-5.3-codex-spark",
        "value": "gpt-5.3-codex-spark",
        "display": "GPT-5.3 Codex Spark",
        "desc": "Ultra-fast coding model",
    },
    {
        "name": "gpt-5.2-codex",
        "value": "gpt-5.2-codex",
        "display": "GPT-5.2 Codex",
        "desc": "Frontier agentic coding model",
    },
    {
        "name": "gpt-5.1-codex-max",
        "value": "gpt-5.1-codex-max",
        "display": "GPT-5.1 Codex Max",
        "desc": "Codex-optimized flagship for deep and fast reasoning",
    },
    {
        "name": "gpt-5.2",
        "value": "gpt-5.2",
        "display": "GPT-5.2",
        "desc": "Latest frontier model with improvements across knowledge, reasoning and coding",
    },
    {
        "name": "gpt-5.1-codex-mini",
        "value": "gpt-5.1-codex-mini",
        "display": "GPT-5.1 Codex Mini",
        "desc": "Optimized for codex. Cheaper, faster, but less capable",
    },
)

_CODEX_EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra-High",
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

    base_name, effort = parse_model_effort(normalized)
    if base_name in _CLAUDE_MODEL_ALIASES:
        resolved_base = _CLAUDE_MODEL_ALIASES[base_name]
    else:
        resolved_base = _CODEX_MODEL_ALIASES.get(base_name, base_name)

    if resolved_base is None:
        return None
    if effort and looks_like_codex_model(resolved_base):
        return f"{resolved_base}-{effort}"
    return resolved_base


def get_claude_model_options() -> list[dict[str, str | None]]:
    """Return Claude model picker options."""
    return [dict(option) for option in _CLAUDE_MODEL_OPTIONS]


def get_codex_model_options() -> list[dict[str, str | None]]:
    """Return Codex model picker options including effort variants."""
    base_models = [dict(option) for option in _CODEX_BASE_MODEL_OPTIONS]
    effort_variants: list[dict[str, str | None]] = []
    for model in base_models:
        model_name = model["name"]
        model_value = model["value"]
        model_display = model["display"]
        model_desc = model["desc"]
        if not model_name or not model_value or not model_display:
            continue
        for effort_key, effort_label in _CODEX_EFFORT_LABELS.items():
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
    if lowered in _CLAUDE_DEFAULT_ALIASES:
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
