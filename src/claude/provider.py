"""Claude backend provider for the multi-backend registry."""

from typing import Any, Optional

from src.backends.models import ModelDefinition, ModelTier

CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max", "auto")

_EFFORT_SUFFIX_MAP: dict[str, str] = {
    "-medium": "medium",
    "-high": "high",
    "-low": "low",
    "-max": "max",
    "-auto": "auto",
}


class ClaudeBackendProvider:
    """Backend provider for Claude Code CLI.

    Registers Claude models and handles Claude-specific effort parsing.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    @property
    def backend_id(self) -> str:
        return "claude"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    def get_executor(self) -> Any:
        return self._executor

    def get_models(self) -> list[ModelDefinition]:
        return [
            ModelDefinition(
                id="claude-opus-4-6",
                backend_id="claude",
                display_name="Opus 4.6",
                aliases=(
                    "opus",
                    "opus-4.6",
                    "claude-opus-4-6",
                    "default",
                    "default (recommended)",
                    "recommended",
                ),
                description="Most capable for complex work",
                tier=ModelTier.REASONING,
                quality_score=5,
                is_default=True,
                cli_value=None,
                supports_effort=True,
                effort_levels=CLAUDE_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-opus-4-6-1m",
                backend_id="claude",
                display_name="Opus 4.6 (1M context)",
                aliases=("opus-1m", "opus (1m context)", "claude-opus-4-6[1m]"),
                description="Opus with extended 1M token context",
                tier=ModelTier.REASONING,
                quality_score=5,
                cli_value="claude-opus-4-6[1m]",
                supports_effort=True,
                effort_levels=CLAUDE_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-sonnet-4-6",
                backend_id="claude",
                display_name="Sonnet 4.6",
                aliases=("sonnet", "sonnet-4.6", "claude-sonnet-4-6"),
                description="Best for everyday tasks",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="sonnet",
                supports_effort=True,
                effort_levels=CLAUDE_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-sonnet-4-6-1m",
                backend_id="claude",
                display_name="Sonnet 4.6 (1M context)",
                aliases=("sonnet-1m", "sonnet (1m context)", "claude-sonnet-4-6[1m]"),
                description="Sonnet with extended 1M token context",
                tier=ModelTier.COMPLEX,
                quality_score=4,
                cli_value="claude-sonnet-4-6[1m]",
                supports_effort=True,
                effort_levels=CLAUDE_EFFORT_LEVELS,
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-haiku-4-5",
                backend_id="claude",
                display_name="Haiku 4.5",
                aliases=("haiku", "haiku-4.5", "claude-haiku-4-5"),
                description="Fastest for quick answers",
                tier=ModelTier.FAST,
                quality_score=2,
                cli_value="haiku",
                capability_reasoning=False,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-opus-4-5",
                backend_id="claude",
                display_name="Opus 4.5",
                aliases=("opus-4.5",),
                description="Previous Opus generation",
                tier=ModelTier.REASONING,
                quality_score=4,
                cli_value="claude-opus-4-5",
                capability_reasoning=True,
                capability_code=True,
            ),
            ModelDefinition(
                id="claude-sonnet-4-5",
                backend_id="claude",
                display_name="Sonnet 4.5",
                aliases=("sonnet-4.5",),
                description="Previous Sonnet generation",
                tier=ModelTier.STANDARD,
                quality_score=3,
                cli_value="claude-sonnet-4-5",
                capability_reasoning=True,
                capability_code=True,
            ),
        ]

    def get_session_id_field(self) -> str:
        return "claude_session_id"

    def parse_effort(self, model: str) -> tuple[str, Optional[str]]:
        """Parse Claude effort suffix from model string.

        Examples
        --------
        "claude-opus-4-6-high" -> ("claude-opus-4-6", "high")
        "claude-opus-4-6-max" -> ("claude-opus-4-6", "max")
        "sonnet" -> ("sonnet", None)
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
