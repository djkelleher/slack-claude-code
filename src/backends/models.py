"""Core data structures for the multi-backend model registry."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ModelTier(Enum):
    """Request complexity tiers for smart routing.

    Modeled after Manifest's tier system (packages/shared/src/tiers.ts).
    """

    FAST = "fast"
    STANDARD = "standard"
    COMPLEX = "complex"
    REASONING = "reasoning"


TIER_LABELS: dict[ModelTier, str] = {
    ModelTier.FAST: "Fast",
    ModelTier.STANDARD: "Standard",
    ModelTier.COMPLEX: "Complex",
    ModelTier.REASONING: "Reasoning",
}

TIER_DESCRIPTIONS: dict[ModelTier, str] = {
    ModelTier.FAST: "Heartbeats, greetings, and low-cost tasks that any model can handle.",
    ModelTier.STANDARD: "General-purpose requests that need a good balance of quality and cost.",
    ModelTier.COMPLEX: "Tasks requiring high quality, nuance, or multi-step reasoning.",
    ModelTier.REASONING: "Advanced reasoning, planning, and critical decision-making.",
}


@dataclass(frozen=True)
class ModelDefinition:
    """Single source of truth for a model.

    Inspired by Manifest's DiscoveredModel interface
    (packages/backend/src/routing/model-discovery/model-fetcher.ts).

    Parameters
    ----------
    id : str
        Canonical model identifier (e.g., "claude-opus-4-6").
    backend_id : str
        Backend that serves this model (e.g., "claude", "codex").
    display_name : str
        Human-readable name for UI (e.g., "Opus 4.6").
    aliases : tuple[str, ...]
        Alternative names users can type (e.g., ("opus", "opus-4.6")).
    description : str
        One-liner for model picker UI.
    tier : ModelTier
        Default tier assignment for smart routing.
    quality_score : int
        Quality rating 1-5, following Manifest's computeQualityScore scheme.
    is_default : bool
        Whether this is the default model for its backend.
    cli_value : Optional[str]
        Value passed to the CLI. None means omit the model flag (use CLI default).
    supports_effort : bool
        Whether this model supports effort level suffixes.
    effort_levels : tuple[str, ...]
        Valid effort levels for this model.
    capability_reasoning : bool
        Whether the model has advanced reasoning capabilities.
    capability_code : bool
        Whether the model has code generation capabilities.
    """

    id: str
    backend_id: str
    display_name: str
    aliases: tuple[str, ...]
    description: str
    tier: ModelTier
    quality_score: int
    is_default: bool = False
    cli_value: Optional[str] = None
    supports_effort: bool = False
    effort_levels: tuple[str, ...] = ()
    capability_reasoning: bool = False
    capability_code: bool = True
