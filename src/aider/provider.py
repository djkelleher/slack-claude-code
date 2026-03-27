"""Aider backend provider for the multi-backend registry.

Aider is a multi-model AI coding assistant CLI (https://aider.chat).
It supports 30+ model providers and operates on git repositories
with automatic commits, repo mapping, and multi-file editing.
"""

from typing import Any, Optional

from src.backends.models import ModelDefinition, ModelTier


class AiderBackendProvider:
    """Backend provider for Aider CLI.

    Aider supports many models from different providers (OpenAI, Anthropic,
    Google, DeepSeek, etc.). The models registered here are the ones that
    perform best on Aider's coding benchmarks. Users can specify any
    model supported by Aider via ``--model``.
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
                id="aider/sonnet",
                backend_id="aider",
                display_name="Aider (Sonnet)",
                aliases=("aider", "aider-sonnet"),
                description="Aider with Claude Sonnet - top coding benchmark performer",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                is_default=True,
                cli_value="sonnet",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/opus",
                backend_id="aider",
                display_name="Aider (Opus)",
                aliases=("aider-opus",),
                description="Aider with Claude Opus for complex reasoning tasks",
                tier=ModelTier.REASONING,
                quality_score=5,
                cli_value="opus",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/deepseek",
                backend_id="aider",
                display_name="Aider (DeepSeek)",
                aliases=("aider-deepseek",),
                description="Aider with DeepSeek Chat V3 - strong open-source model",
                tier=ModelTier.STANDARD,
                quality_score=3,
                cli_value="deepseek",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/gpt-4o",
                backend_id="aider",
                display_name="Aider (GPT-4o)",
                aliases=("aider-gpt4o", "aider-4o"),
                description="Aider with OpenAI GPT-4o",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="gpt-4o",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/gemini",
                backend_id="aider",
                display_name="Aider (Gemini Pro)",
                aliases=("aider-gemini",),
                description="Aider with Google Gemini 2.5 Pro",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="gemini/gemini-2.5-pro-preview-05-06",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="aider/haiku",
                backend_id="aider",
                display_name="Aider (Haiku)",
                aliases=("aider-haiku",),
                description="Aider with Claude Haiku for fast lightweight tasks",
                tier=ModelTier.FAST,
                quality_score=2,
                cli_value="haiku",
                capability_reasoning=False,
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
