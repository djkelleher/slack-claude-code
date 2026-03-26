"""Backend provider protocol for the multi-backend registry.

Inspired by Manifest's PROVIDER_REGISTRY pattern
(packages/backend/src/common/constants/providers.ts).
"""

from typing import Any, Optional, Protocol, runtime_checkable

from src.backends.models import ModelDefinition


@runtime_checkable
class BackendProvider(Protocol):
    """Protocol that each backend must implement.

    Each backend (Claude CLI, Codex, Gemini, etc.) provides a provider class
    that registers its models, wraps its executor, and handles backend-specific
    parsing (e.g., effort suffixes).
    """

    @property
    def backend_id(self) -> str:
        """Unique identifier for this backend (e.g., "claude", "codex")."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name for UI (e.g., "Claude Code", "OpenAI Codex")."""
        ...

    def get_executor(self) -> Any:
        """Return the subprocess executor instance for this backend."""
        ...

    def get_models(self) -> list[ModelDefinition]:
        """Return the static list of models this backend supports."""
        ...

    def get_session_id_field(self) -> str:
        """Return the Session field name for this backend's session ID.

        Returns
        -------
        str
            e.g., "claude_session_id" or "codex_session_id"
        """
        ...

    def parse_effort(self, model: str) -> tuple[str, Optional[str]]:
        """Parse effort suffix from a model name string.

        Parameters
        ----------
        model : str
            Model name, possibly with effort suffix (e.g., "claude-opus-4-6-high").

        Returns
        -------
        tuple[str, Optional[str]]
            (base_model, effort_level) -- effort_level is None if no suffix found.
        """
        ...

    def format_model_with_effort(self, base_model: str, effort: str) -> str:
        """Combine a base model and effort level into a CLI-ready model string.

        Parameters
        ----------
        base_model : str
            The base model identifier.
        effort : str
            The effort level to apply.

        Returns
        -------
        str
            Combined model string (e.g., "claude-opus-4-6-high").
        """
        ...

    async def discover_models(self) -> list[ModelDefinition]:
        """Discover available models at runtime.

        Default implementations should return ``get_models()``.
        HTTP-API backends can call provider ``/models`` endpoints.

        Returns
        -------
        list[ModelDefinition]
            Discovered models, potentially including dynamically available ones.
        """
        ...
