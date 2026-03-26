"""Gemini backend provider for the multi-backend registry."""

from typing import Any, Optional

from src.backends.models import ModelDefinition, ModelTier


class GeminiBackendProvider:
    """Backend provider for Google Gemini CLI.

    Proof-of-concept backend demonstrating how to add a new AI CLI tool
    to the registry system. The Gemini CLI is only registered when the
    ``gemini`` binary is found on PATH.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    @property
    def backend_id(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Gemini CLI"

    def get_executor(self) -> Any:
        return self._executor

    def get_models(self) -> list[ModelDefinition]:
        return [
            ModelDefinition(
                id="gemini-2.5-pro",
                backend_id="gemini",
                display_name="Gemini 2.5 Pro",
                aliases=("gemini", "gemini-pro"),
                description="Google's frontier coding model",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                is_default=True,
                cli_value="gemini-2.5-pro",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gemini-2.5-flash",
                backend_id="gemini",
                display_name="Gemini 2.5 Flash",
                aliases=("gemini-flash",),
                description="Fast and efficient Gemini model",
                tier=ModelTier.STANDARD,
                quality_score=3,
                cli_value="gemini-2.5-flash",
                capability_reasoning=False,
                capability_code=True,
            ),
        ]

    def get_session_id_field(self) -> str:
        return "gemini_session_id"

    def parse_effort(self, model: str) -> tuple[str, Optional[str]]:
        """Gemini CLI does not support effort levels."""
        return model.strip(), None

    def format_model_with_effort(self, base_model: str, effort: str) -> str:
        return base_model

    async def discover_models(self) -> list[ModelDefinition]:
        return self.get_models()
