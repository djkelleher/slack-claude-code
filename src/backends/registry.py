"""Central registry for backends and models.

Inspired by Manifest's derived lookup maps (PROVIDER_BY_ID, PROVIDER_BY_ID_OR_ALIAS)
in packages/backend/src/common/constants/providers.ts, but implemented as a mutable
class to support dynamic model discovery.
"""

from typing import Optional

from loguru import logger

from src.backends.backend_provider import BackendProvider
from src.backends.models import ModelDefinition, ModelTier


class BackendRegistry:
    """Central registry for backend providers and their models.

    Provides unified model resolution, alias lookup, and backend dispatch.
    Replaces the scattered model dicts in config.py with a single source of truth.
    """

    def __init__(self) -> None:
        self._backends: dict[str, BackendProvider] = {}
        self._models: dict[str, ModelDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._default_model_id: Optional[str] = None

    def register_backend(self, provider: BackendProvider) -> None:
        """Register a backend provider and all its models.

        Parameters
        ----------
        provider : BackendProvider
            The backend provider to register.
        """
        backend_id = provider.backend_id
        if backend_id in self._backends:
            logger.warning(f"Overwriting existing backend: {backend_id}")
        self._backends[backend_id] = provider

        for model in provider.get_models():
            self._register_model(model)

    def _register_model(self, model: ModelDefinition) -> None:
        """Register a single model and its aliases."""
        if model.id in self._models:
            logger.debug(f"Overwriting model: {model.id}")
        self._models[model.id] = model

        self._aliases[model.id.lower()] = model.id
        for alias in model.aliases:
            alias_lower = alias.lower()
            if alias_lower in self._aliases and self._aliases[alias_lower] != model.id:
                logger.debug(
                    f"Alias '{alias}' remapped from {self._aliases[alias_lower]} to {model.id}"
                )
            self._aliases[alias_lower] = model.id

        if model.is_default and (
            self._default_model_id is None
            or self._models[self._default_model_id].backend_id == model.backend_id
        ):
            self._default_model_id = model.id

    def get_backend(self, backend_id: str) -> Optional[BackendProvider]:
        """Look up a backend provider by ID.

        Parameters
        ----------
        backend_id : str
            The backend identifier (e.g., "claude", "codex").

        Returns
        -------
        Optional[BackendProvider]
            The provider, or None if not registered.
        """
        return self._backends.get(backend_id)

    def resolve_model(self, user_input: str) -> Optional[ModelDefinition]:
        """Resolve user input to a ModelDefinition.

        Handles alias lookup and effort suffix stripping. Does NOT handle effort
        application — callers should use the provider's parse_effort for that.

        Parameters
        ----------
        user_input : str
            Raw user input (e.g., "opus", "gpt-5.3-codex", "sonnet-4.6").

        Returns
        -------
        Optional[ModelDefinition]
            The resolved model, or None if no match found.
        """
        normalized = user_input.strip().lower()
        if not normalized:
            return self._models.get(self._default_model_id) if self._default_model_id else None

        canonical_id = self._aliases.get(normalized)
        if canonical_id:
            return self._models.get(canonical_id)

        # Try stripping effort suffixes via each backend's parser
        for backend in self._backends.values():
            base_model, effort = backend.parse_effort(normalized)
            if effort:
                canonical_id = self._aliases.get(base_model.lower())
                if canonical_id:
                    return self._models.get(canonical_id)

        return None

    def get_backend_for_model(self, model: Optional[str]) -> str:
        """Determine which backend to use for a model name.

        Drop-in replacement for config.get_backend_for_model().

        Parameters
        ----------
        model : Optional[str]
            Model name or alias. None returns the default backend.

        Returns
        -------
        str
            Backend identifier (e.g., "claude", "codex").
        """
        if model is None:
            if self._default_model_id:
                return self._models[self._default_model_id].backend_id
            return "claude"

        resolved = self.resolve_model(model)
        if resolved:
            return resolved.backend_id

        normalized = model.strip().lower()
        if normalized.startswith("gpt-") or normalized.startswith("codex"):
            return "codex"
        if normalized.startswith("claude"):
            return "claude"
        if normalized.startswith("gemini"):
            return "gemini"

        return "claude"

    def get_display_name(self, model: Optional[str]) -> str:
        """Return human-readable display name for a model.

        Parameters
        ----------
        model : Optional[str]
            Model name or alias.

        Returns
        -------
        str
            Display name (e.g., "Opus 4.6").
        """
        if model is None:
            if self._default_model_id:
                return self._models[self._default_model_id].display_name
            return "Unknown"

        resolved = self.resolve_model(model)
        if resolved:
            return resolved.display_name

        return model

    def get_all_models(self) -> list[ModelDefinition]:
        """Return all registered models.

        Returns
        -------
        list[ModelDefinition]
            All models, ordered by backend then quality score descending.
        """
        return sorted(
            self._models.values(),
            key=lambda m: (m.backend_id, -m.quality_score),
        )

    def get_models_for_backend(self, backend_id: str) -> list[ModelDefinition]:
        """Return models for a specific backend.

        Parameters
        ----------
        backend_id : str
            The backend identifier.

        Returns
        -------
        list[ModelDefinition]
            Models for this backend, ordered by quality score descending.
        """
        return sorted(
            [m for m in self._models.values() if m.backend_id == backend_id],
            key=lambda m: -m.quality_score,
        )

    def get_models_for_tier(self, tier: ModelTier) -> list[ModelDefinition]:
        """Return models assigned to a specific tier.

        Parameters
        ----------
        tier : ModelTier
            The tier to filter by.

        Returns
        -------
        list[ModelDefinition]
            Models at this tier, ordered by quality score descending.
        """
        return sorted(
            [m for m in self._models.values() if m.tier == tier],
            key=lambda m: -m.quality_score,
        )

    def get_default_model(self) -> Optional[ModelDefinition]:
        """Return the global default model.

        Returns
        -------
        Optional[ModelDefinition]
            The default model, or None if no default is set.
        """
        if self._default_model_id:
            return self._models.get(self._default_model_id)
        return None

    @property
    def backend_ids(self) -> list[str]:
        """Return all registered backend IDs."""
        return list(self._backends.keys())

    async def discover_all_models(self) -> None:
        """Call discover_models() on each backend and merge results.

        Like Manifest's ModelDiscoveryService.getModelsForAgent().
        """
        for backend_id, provider in self._backends.items():
            try:
                discovered = await provider.discover_models()
                for model in discovered:
                    if model.id not in self._models:
                        self._register_model(model)
                        logger.info(f"Discovered new model: {model.id} ({backend_id})")
            except Exception as exc:
                logger.warning(f"Model discovery failed for backend {backend_id}: {exc}")

    async def refresh_models(self, backend_id: str) -> list[ModelDefinition]:
        """Re-discover models for a specific backend.

        Parameters
        ----------
        backend_id : str
            The backend to refresh.

        Returns
        -------
        list[ModelDefinition]
            Newly discovered models.
        """
        provider = self._backends.get(backend_id)
        if not provider:
            return []

        new_models: list[ModelDefinition] = []
        try:
            discovered = await provider.discover_models()
            for model in discovered:
                if model.id not in self._models:
                    self._register_model(model)
                    new_models.append(model)
                    logger.info(f"Discovered new model: {model.id} ({backend_id})")
        except Exception as exc:
            logger.warning(f"Model refresh failed for backend {backend_id}: {exc}")

        return new_models
