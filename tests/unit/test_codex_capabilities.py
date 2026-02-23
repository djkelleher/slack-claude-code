"""Unit tests for Codex capability mappings."""

from src.codex.capabilities import (
    codex_mode_alias_for_approval,
    get_codex_hint_for_claude_command,
    is_claude_only_slash_command,
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)


class TestCodexModeMappings:
    """Tests for `/mode` compatibility behavior in Codex sessions."""

    def test_bypass_maps_to_never(self):
        """bypass alias maps to approval=never."""
        resolved = resolve_codex_compat_mode("bypass")
        assert resolved.approval_mode == "never"
        assert resolved.error is None

    def test_ask_and_default_map_to_on_request(self):
        """ask/default aliases map to approval=on-request."""
        ask = resolve_codex_compat_mode("ask")
        default = resolve_codex_compat_mode("default")

        assert ask.approval_mode == "on-request"
        assert default.approval_mode == "on-request"

    def test_plan_is_reported_as_unsupported(self):
        """plan alias returns a compatibility error for Codex."""
        resolved = resolve_codex_compat_mode("plan")
        assert resolved.approval_mode is None
        assert resolved.error is not None
        assert "Claude-specific" in resolved.error

    def test_approval_mode_normalization(self):
        """Deprecated approvals are normalized to supported values."""
        assert normalize_codex_approval_mode("on-failure") == "on-request"
        assert normalize_codex_approval_mode("never") == "never"

    def test_mode_alias_derivation(self):
        """Best-effort compatibility alias derives from approval mode."""
        assert codex_mode_alias_for_approval("never") == "bypass"
        assert codex_mode_alias_for_approval("on-request") == "ask"


class TestCodexCommandHints:
    """Tests for Claude-only command hints in Codex sessions."""

    def test_claude_only_command_detection(self):
        """Known Claude-only slash commands are detected."""
        assert is_claude_only_slash_command("/cost") is True
        assert is_claude_only_slash_command("/approval") is False

    def test_hint_generation(self):
        """A Codex hint is returned for Claude-only commands."""
        hint = get_codex_hint_for_claude_command("/cost")
        assert "/codex-status" in hint
