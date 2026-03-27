"""Aider backend provider for the multi-backend registry.

Aider is a multi-model AI coding assistant CLI (https://aider.chat).
It supports 30+ model providers and operates on git repositories
with automatic commits, repo mapping, and multi-file editing.

Only models that aren't already reachable through the Claude or Codex
backends are registered here. Aider's value is access to providers
like DeepSeek, Ollama (local models), and OpenRouter that the other
backends don't support.
"""

from typing import Any, Optional

from src.backends.models import ModelDefinition, ModelTier


class AiderBackendProvider:
    """Backend provider for Aider CLI.

    Registers only models from providers not covered by Claude or Codex
    backends. Users can still specify any Aider-supported model via the
    full ``--model`` syntax (e.g., ``/model aider/openrouter/meta-llama/...``).
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    @property
    def backend_id(self) -> str:
        return "aider"

    @property
    def display_name(self) -> str:
        return "Aider"

    def get_executor(self) -> Any:
        return self._executor

    def get_models(self) -> list[ModelDefinition]:
        return [
            ModelDefinition(
                id="aider/deepseek",
                backend_id="aider",
                display_name="Aider (DeepSeek)",
                aliases=("aider-deepseek",),
                description="Aider with DeepSeek V3 - strong open-source coding model",
                tier=ModelTier.STANDARD,
                quality_score=3,
                is_default=True,
                cli_value="deepseek",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/ollama",
                backend_id="aider",
                display_name="Aider (Ollama)",
                aliases=("aider-ollama", "aider-local"),
                description="Aider with local Ollama models",
                tier=ModelTier.STANDARD,
                quality_score=2,
                cli_value="ollama/qwen2.5-coder:32b",
                capability_reasoning=False,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/openrouter",
                backend_id="aider",
                display_name="Aider (OpenRouter)",
                aliases=("aider-openrouter", "aider-or"),
                description="Aider via OpenRouter - access 300+ models",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="openrouter/anthropic/claude-sonnet-4",
                capability_reasoning=True,
                capability_code=True,
            ),
        ]

    def get_session_id_field(self) -> str:
        return "aider_session_id"

    def parse_effort(self, model: str) -> tuple[str, Optional[str]]:
        """Aider does not support effort levels."""
        return model.strip(), None

    def format_model_with_effort(self, base_model: str, effort: str) -> str:
        return base_model

    async def discover_models(self) -> list[ModelDefinition]:
        return self.get_models()
