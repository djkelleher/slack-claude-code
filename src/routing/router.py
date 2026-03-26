"""Model router for smart tier-based model selection.

Uses the request scorer to determine complexity, then selects the best
model at the appropriate tier using Manifest's pickBest() algorithm
(packages/backend/src/routing/tier-auto-assign.service.ts).
"""

from typing import Optional

from loguru import logger

from src.backends.models import ModelDefinition, ModelTier
from src.backends.registry import BackendRegistry
from src.routing.scorer import ScoringResult, score_request


class ModelRouter:
    """Routes requests to the best model based on complexity scoring.

    Parameters
    ----------
    registry : BackendRegistry
        The backend registry containing all available models.
    """

    def __init__(self, registry: BackendRegistry) -> None:
        self._registry = registry

    def select_model(
        self,
        prompt: str,
        *,
        preferred_backend: Optional[str] = None,
        user_model: Optional[str] = None,
    ) -> tuple[ModelDefinition, ScoringResult]:
        """Select the best model for a request.

        Parameters
        ----------
        prompt : str
            The user's request text.
        preferred_backend : Optional[str]
            If set, only consider models from this backend.
        user_model : Optional[str]
            If set, honor this explicit model choice (bypasses scoring).

        Returns
        -------
        tuple[ModelDefinition, ScoringResult]
            The selected model and the scoring result that led to it.
        """
        scoring = score_request(prompt)

        # Explicit user choice always wins (like Manifest's override_model)
        if user_model:
            resolved = self._registry.resolve_model(user_model)
            if resolved:
                return resolved, scoring

        # Pick best model at the scored tier
        selected = self._pick_best_for_tier(
            scoring.tier,
            preferred_backend=preferred_backend,
        )

        if selected:
            logger.debug(
                f"Smart routing: tier={scoring.tier.value} score={scoring.score:.3f} "
                f"confidence={scoring.confidence:.2f} -> {selected.display_name}"
            )
            return selected, scoring

        # Fallback: try stepping up tiers until we find a model
        for fallback_tier in _TIER_FALLBACK_ORDER.get(scoring.tier, []):
            selected = self._pick_best_for_tier(
                fallback_tier,
                preferred_backend=preferred_backend,
            )
            if selected:
                logger.debug(
                    f"Smart routing fallback: {scoring.tier.value} -> {fallback_tier.value} "
                    f"-> {selected.display_name}"
                )
                return selected, scoring

        # Ultimate fallback: default model
        default = self._registry.get_default_model()
        if default:
            return default, scoring

        raise ValueError("No models available in registry for routing")

    def _pick_best_for_tier(
        self,
        tier: ModelTier,
        preferred_backend: Optional[str] = None,
    ) -> Optional[ModelDefinition]:
        """Select the best model for a tier using Manifest's pickBest() algorithm.

        Algorithm per tier:
        - FAST: cheapest model (lowest quality score as proxy for cost)
        - STANDARD: cheapest among quality >= 2
        - COMPLEX: highest quality, cost breaks ties
        - REASONING: highest quality among reasoning-capable

        Parameters
        ----------
        tier : ModelTier
            The target tier.
        preferred_backend : Optional[str]
            If set, only consider models from this backend.

        Returns
        -------
        Optional[ModelDefinition]
            The best model, or None if no candidates.
        """
        candidates = self._registry.get_all_models()
        if preferred_backend:
            candidates = [m for m in candidates if m.backend_id == preferred_backend]

        if not candidates:
            return None

        # Sort by quality ascending (cheapest first, as proxy for cost)
        by_quality_asc = sorted(candidates, key=lambda m: m.quality_score)

        if tier == ModelTier.FAST:
            # Cheapest model (lowest quality score)
            return by_quality_asc[0]

        if tier == ModelTier.STANDARD:
            # Cheapest with quality >= 2
            eligible = [m for m in by_quality_asc if m.quality_score >= 2]
            return eligible[0] if eligible else by_quality_asc[0]

        if tier == ModelTier.COMPLEX:
            # Highest quality (cost breaks ties via stable sort)
            by_quality_desc = sorted(candidates, key=lambda m: -m.quality_score)
            return by_quality_desc[0]

        if tier == ModelTier.REASONING:
            # Highest quality among reasoning-capable
            reasoning_models = [m for m in candidates if m.capability_reasoning]
            if reasoning_models:
                by_quality_desc = sorted(reasoning_models, key=lambda m: -m.quality_score)
                return by_quality_desc[0]
            # Fall back to highest quality overall
            by_quality_desc = sorted(candidates, key=lambda m: -m.quality_score)
            return by_quality_desc[0]

        return by_quality_asc[0]


# Fallback tier order: if no models at target tier, try these
_TIER_FALLBACK_ORDER: dict[ModelTier, list[ModelTier]] = {
    ModelTier.FAST: [ModelTier.STANDARD, ModelTier.COMPLEX],
    ModelTier.STANDARD: [ModelTier.COMPLEX, ModelTier.FAST],
    ModelTier.COMPLEX: [ModelTier.REASONING, ModelTier.STANDARD],
    ModelTier.REASONING: [ModelTier.COMPLEX, ModelTier.STANDARD],
}
