"""Codex backend provider for the multi-backend registry."""

from typing import Any, Optional

from src.backends.models import ModelDefinition, ModelTier

CODEX_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")

_EFFORT_SUFFIX_MAP: dict[str, str] = {
    "-extra-high": "xhigh",
    "-extra_high": "xhigh",
    "-extrahigh": "xhigh",
    "-xhigh": "xhigh",
    "-medium": "medium",
    "-high": "high",
    "-low": "low",
}


class CodexBackendProvider:
    """Backend provider for OpenAI Codex (app-server JSON-RPC).

    Registers Codex models and handles Codex-specific effort parsing.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    @property
    def backend_id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex"

    def get_executor(self) -> Any:
        return self._executor

    def get_models(self) -> list[ModelDefinition]:
        return [
            ModelDefinition(
                id="gpt-5.3-codex",
                backend_id="codex",
                display_name="GPT-5.3 Codex",
                aliases=("codex",),
                description="Latest frontier agentic coding model",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                is_default=True,
                cli_value="gpt-5.3-codex",
                supports_effort=True,
                effort_levels=CODEX_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.4",
                backend_id="codex",
                display_name="GPT-5.4",
                aliases=("g54",),
                description="Latest frontier agentic coding model",
                tier=ModelTier.REASONING,
                quality_score=5,
                cli_value="gpt-5.4",
                supports_effort=True,
                effort_levels=CODEX_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.3-codex-spark",
                backend_id="codex",
                display_name="GPT-5.3 Codex Spark",
                aliases=(),
                description="Ultra-fast coding model",
                tier=ModelTier.FAST,
                quality_score=2,
                cli_value="gpt-5.3-codex-spark",
                capability_reasoning=False,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.2-codex",
                backend_id="codex",
                display_name="GPT-5.2 Codex",
                aliases=(),
                description="Frontier agentic coding model",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="gpt-5.2-codex",
                supports_effort=True,
                effort_levels=CODEX_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.1-codex-max",
                backend_id="codex",
                display_name="GPT-5.1 Codex Max",
                aliases=(),
                description="Codex-optimized flagship for deep and fast reasoning",
                tier=ModelTier.REASONING,
                quality_score=5,
                cli_value="gpt-5.1-codex-max",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.2",
                backend_id="codex",
                display_name="GPT-5.2",
                aliases=(),
                description="Frontier model with improvements across knowledge, reasoning and coding",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="gpt-5.2",
                supports_effort=True,
                effort_levels=CODEX_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="gpt-5.1-codex-mini",
                backend_id="codex",
                display_name="GPT-5.1 Codex Mini",
                aliases=(),
                description="Optimized for codex. Cheaper, faster, but less capable",
                tier=ModelTier.STANDARD,
                quality_score=2,
                cli_value="gpt-5.1-codex-mini",
                capability_reasoning=False,
                capability_code=True,
            ),
        ]

    def get_session_id_field(self) -> str:
        return "codex_session_id"

    def parse_effort(self, model: str) -> tuple[str, Optional[str]]:
        """Parse Codex effort suffix from model string.

        Examples
        --------
        "gpt-5.3-codex-high" -> ("gpt-5.3-codex", "high")
        "gpt-5.3-codex-extra-high" -> ("gpt-5.3-codex", "xhigh")
        "gpt-5.3-codex" -> ("gpt-5.3-codex", None)
        """
        model_clean = model.strip()
        model_lower = model_clean.lower()
        for suffix, level in _EFFORT_SUFFIX_MAP.items():
            if model_lower.endswith(suffix):
                return model_clean[: -len(suffix)], level
        return model_clean, None

    def format_model_with_effort(self, base_model: str, effort: str) -> str:
        return f"{base_model}-{effort}"

    async def discover_models(self) -> list[ModelDefinition]:
        return self.get_models()
