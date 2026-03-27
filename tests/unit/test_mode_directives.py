"""Unit tests for runtime mode directive parsing and resolution."""

import pytest

from src.utils.mode_directives import (
    ModeDirectiveError,
    map_codex_alias_to_permission_mode,
    parse_parenthesized_mode_directive_line,
    resolve_runtime_mode_directives,
    resolve_runtime_mode_value,
)


def test_parse_parenthesized_mode_directive_line_extracts_value() -> None:
    assert parse_parenthesized_mode_directive_line("(mode: plan)") == "plan"
    assert (
        parse_parenthesized_mode_directive_line("((mode: sandbox: workspace-write))")
        == "sandbox: workspace-write"
    )
    assert parse_parenthesized_mode_directive_line("(mode: splan cs46h, g54h)") == (
        "splan cs46h, g54h"
    )


def test_parse_parenthesized_mode_directive_line_returns_none_for_other_directives() -> None:
    assert parse_parenthesized_mode_directive_line("(append)") is None
    assert parse_parenthesized_mode_directive_line("plain text") is None


def test_parse_parenthesized_mode_directive_line_rejects_empty_double_paren_value() -> None:
    with pytest.raises(ModeDirectiveError, match="Mode directive must include a mode value"):
        parse_parenthesized_mode_directive_line("((mode: ))")


def test_map_codex_alias_to_permission_mode() -> None:
    assert map_codex_alias_to_permission_mode("bypass") == "bypassPermissions"
    assert map_codex_alias_to_permission_mode("plan") == "plan"
    assert map_codex_alias_to_permission_mode("default") == "default"


def test_resolve_runtime_mode_value_codex_alias_sets_permission_and_approval() -> None:
    resolved = resolve_runtime_mode_value("bypass", backend="codex")
    assert resolved.permission_mode == "bypassPermissions"
    assert resolved.approval_mode == "never"
    assert resolved.sandbox_mode is None


def test_resolve_runtime_mode_value_codex_sandbox_directive() -> None:
    resolved = resolve_runtime_mode_value("sandbox: read-only", backend="codex")
    assert resolved.permission_mode is None
    assert resolved.approval_mode is None
    assert resolved.sandbox_mode == "read-only"


def test_resolve_runtime_mode_value_rejects_codex_only_directives_for_claude() -> None:
    with pytest.raises(ModeDirectiveError, match="only supported for Codex sessions"):
        resolve_runtime_mode_value("approval: never", backend="claude")


def test_resolve_runtime_mode_value_rejects_unknown_claude_mode_alias() -> None:
    with pytest.raises(ModeDirectiveError, match="Unknown mode"):
        resolve_runtime_mode_value("fast", backend="claude")


def test_resolve_runtime_mode_directives_supports_semicolon_subdirectives() -> None:
    resolved = resolve_runtime_mode_directives(
        "splan cs46h, g54h; approval: on-request; sandbox: workspace-write",
        backend="codex",
    )
    assert resolved.plan_mode is not None
    assert resolved.plan_mode.strategy == "splan"
    assert resolved.plan_mode.models == ("claude-sonnet-4-6-high", "gpt-5.4-high")
    assert resolved.overrides.approval_mode == "on-request"
    assert resolved.overrides.sandbox_mode == "workspace-write"


def test_resolve_runtime_mode_directives_supports_real_semicolon_mix_in_queue_context() -> None:
    resolved = resolve_runtime_mode_directives(
        "plan; approval: never; sandbox: read-only",
        backend="codex",
    )
    assert resolved.plan_mode is None
    assert resolved.overrides.permission_mode == "plan"
    assert resolved.overrides.approval_mode == "never"
    assert resolved.overrides.sandbox_mode == "read-only"


def test_resolve_runtime_mode_directives_rejects_legacy_adversary_keys() -> None:
    with pytest.raises(ModeDirectiveError, match="renamed"):
        resolve_runtime_mode_directives("advs: cs46h, g54h", backend="codex")


def test_resolve_runtime_mode_directives_rejects_old_splan_colon_format() -> None:
    with pytest.raises(ModeDirectiveError, match="Unsupported plan strategy syntax"):
        resolve_runtime_mode_directives("splan: cs46h, g54h", backend="codex")


def test_resolve_runtime_mode_directives_rejects_old_sandbox_space_format() -> None:
    with pytest.raises(ModeDirectiveError, match="Use `sandbox: <mode>`"):
        resolve_runtime_mode_directives("sandbox read-only", backend="codex")
