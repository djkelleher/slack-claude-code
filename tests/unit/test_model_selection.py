"""Unit tests for shared model selection helpers."""

from src.utils.model_selection import (
    apply_effort_to_model,
    backend_label_for_model,
    codex_model_validation_error,
    effort_display_name,
    get_all_model_options,
    get_claude_model_options,
    get_codex_model_options,
    get_effort_options,
    model_display_name,
    normalize_current_model,
    normalize_effort_name,
    normalize_model_name,
    resolve_model_selection_action,
    split_model_and_effort,
    split_model_input_and_effort,
)


class TestNormalizeModelName:
    """Tests for model alias normalization."""

    def test_normalizes_claude_default_aliases(self):
        """Default aliases should normalize to None."""
        assert normalize_model_name("default") is None
        assert normalize_model_name("opus") is None
        assert normalize_model_name("recommended") is None

    def test_normalizes_claude_named_aliases(self):
        """Claude aliases should map to canonical Claude IDs."""
        assert normalize_model_name("sonnet") == "sonnet"
        assert normalize_model_name("sonnet-4.5") == "claude-sonnet-4-5"
        assert normalize_model_name("sonnet-1m") == "claude-sonnet-4-6[1m]"
        assert normalize_model_name("haiku-4.5") == "haiku"
        assert normalize_model_name("opus-4.5") == "claude-opus-4-5"
        assert normalize_model_name("opus-high") == "claude-opus-4-6-high"
        assert normalize_model_name("co46h") == "claude-opus-4-6-high"
        assert normalize_model_name("cs46h") == "claude-sonnet-4-6-high"
        assert normalize_model_name("claude-opus-4-6-max") == "claude-opus-4-6-max"

    def test_normalizes_codex_aliases_with_effort(self):
        """Codex aliases should map to canonical Codex IDs with effort suffixes."""
        assert normalize_model_name("codex") == "gpt-5.3-codex"
        assert normalize_model_name("codex-extra-high") == "gpt-5.3-codex-xhigh"
        assert normalize_model_name("g54h") == "gpt-5.4-high"
        assert (
            normalize_model_name("gpt-5.1-codex-max-high") == "gpt-5.1-codex-max-high"
        )

    def test_passthrough_for_unknown_non_empty_model(self):
        """Unknown model IDs should be preserved."""
        assert normalize_model_name("custom-model-id") == "custom-model-id"


class TestModelDisplayName:
    """Tests for model display helper."""

    def test_known_model_display_name(self):
        """Known model IDs should return friendly labels."""
        assert model_display_name("claude-opus-4-6[1m]") == "Opus 4.6 (1M context)"
        assert model_display_name("haiku") == "Haiku 4.5"
        assert model_display_name("claude-opus-4-5") == "Opus 4.5"
        assert model_display_name("claude-sonnet-4-5") == "Sonnet 4.5"
        assert model_display_name("gpt-5.3-codex") == "GPT-5.3 Codex"
        assert model_display_name("gpt-5.4") == "GPT-5.4"
        assert model_display_name("gpt-5.3-codex-xhigh") == "GPT-5.3 Codex (Extra-High)"

    def test_unknown_model_display_name(self):
        """Unknown model IDs should display raw model name."""
        assert model_display_name("custom-model-id") == "custom-model-id"

    def test_default_model_display_name(self):
        """Default model should display the Opus 4.6 label."""
        assert model_display_name(None) == "Opus 4.6"


class TestCodexModelValidation:
    """Tests for Codex model validation helper."""

    def test_supported_codex_model_returns_no_error(self):
        """Supported Codex IDs should not return validation error."""
        assert codex_model_validation_error("gpt-5.3-codex-high") is None

    def test_invalid_codex_like_model_returns_error(self):
        """Unsupported Codex-like IDs should return formatted error text."""
        error = codex_model_validation_error("gpt-5")
        assert error is not None
        assert "Unsupported Codex model" in error
        assert "Supported Codex models" in error


class TestCurrentModelNormalization:
    """Tests for persisted-model normalization helper."""

    def test_normalize_current_model_defaults_to_none(self):
        """Persisted default aliases should normalize to None."""
        assert normalize_current_model("default") is None
        assert normalize_current_model("opus") is None
        assert normalize_current_model("claude-opus-4-6") is None

    def test_normalize_current_model_lowercases_values(self):
        """Persisted values should normalize to lower-case IDs."""
        assert normalize_current_model("GPT-5.3-CODEX-HIGH") == "gpt-5.3-codex-high"
        assert normalize_current_model("GPT-5.4") == "gpt-5.4"


class TestBackendLabelForModel:
    """Tests for backend label helper."""

    def test_backend_label_for_claude_and_codex(self):
        """Helper should return backend labels used in Slack copy."""
        assert backend_label_for_model(None) == "Claude Code"
        assert backend_label_for_model("gpt-5.3-codex") == "OpenAI Codex"


class TestModelOptionsCatalog:
    """Tests for shared model options catalog helpers."""

    def test_claude_and_codex_options_include_expected_entries(self):
        """Catalog helpers should return known model entries."""
        claude_options = get_claude_model_options()
        codex_options = get_codex_model_options()

        assert any(option["name"] == "opus-4-6" for option in claude_options)
        assert any(option["name"] == "opus-4-5" for option in claude_options)
        assert any(option["name"] == "sonnet-4-5" for option in claude_options)
        assert any(option["name"] == "sonnet" for option in claude_options)
        assert any(option["name"] == "gpt-5.3-codex" for option in codex_options)
        assert any(option["name"] == "gpt-5.4" for option in codex_options)
        assert not any(
            option["name"] == "gpt-5.3-codex-xhigh" for option in codex_options
        )

    def test_get_all_model_options_combines_backends(self):
        """Combined options should contain both Claude and Codex entries."""
        all_options = get_all_model_options()
        names = {option["name"] for option in all_options}
        assert "opus-4-6" in names
        assert "opus-4-5" in names
        assert "sonnet-4-5" in names
        assert "gpt-5.4" in names
        assert "gpt-5.2-codex" in names

    def test_normalizes_new_codex_frontier_model(self):
        """New Codex model IDs should normalize directly."""
        assert normalize_model_name("gpt-5.4") == "gpt-5.4"
        assert normalize_model_name("gpt-5.4-high") == "gpt-5.4-high"


class TestResolveModelSelectionAction:
    """Tests for action-id model resolver."""

    def test_resolves_known_action_key_with_display(self):
        """Known picker action IDs should return canonical value and display name."""
        model_value, display_name = resolve_model_selection_action("gpt-5.3-codex")
        assert model_value == "gpt-5.3-codex"
        assert display_name == "GPT-5.3 Codex"

    def test_resolves_unknown_action_key_via_normalizer(self):
        """Unknown picker action IDs should fall back to model normalization."""
        model_value, display_name = resolve_model_selection_action("codex-extra-high")
        assert model_value == "gpt-5.3-codex-xhigh"
        assert display_name == "GPT-5.3 Codex (Extra-High)"


class TestEffortHelpers:
    """Tests for effort parsing/apply helpers."""

    def test_splits_model_input_with_space_effort(self):
        model_text, effort = split_model_input_and_effort("claude-opus-4-6 high")
        assert model_text == "claude-opus-4-6"
        assert effort == "high"

    def test_splits_model_input_without_effort(self):
        model_text, effort = split_model_input_and_effort("gpt-5.4")
        assert model_text == "gpt-5.4"
        assert effort is None

    def test_apply_effort_to_claude_default(self):
        model_value, error = apply_effort_to_model(None, "max")
        assert error is None
        assert model_value == "claude-opus-4-6-max"

    def test_apply_effort_to_codex_model(self):
        model_value, error = apply_effort_to_model("gpt-5.4", "xhigh")
        assert error is None
        assert model_value == "gpt-5.4-xhigh"

    def test_apply_invalid_effort_for_backend(self):
        model_value, error = apply_effort_to_model("gpt-5.4", "auto")
        assert model_value == "gpt-5.4"
        assert error is not None

    def test_split_model_and_effort_from_persisted_value(self):
        base, effort = split_model_and_effort("claude-opus-4-6-high")
        assert base == "claude-opus-4-6"
        assert effort == "high"

    def test_effort_display_name(self):
        assert effort_display_name(None) == "Standard"
        assert effort_display_name("extra-high") == "Extra-High"

    def test_effort_options_include_standard(self):
        options = get_effort_options()
        assert options[0]["value"] == "none"
        assert any(option["value"] == "xhigh" for option in options)

    def test_normalize_effort_name_aliases(self):
        assert normalize_effort_name("extra-high") == "xhigh"
        assert normalize_effort_name("default") is None
