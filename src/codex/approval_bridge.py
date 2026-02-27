"""Helpers for mapping Codex app-server approvals to Slack permission UX."""

from __future__ import annotations

import json
from typing import Optional


def format_approval_request_for_slack(
    method: str, params: dict
) -> tuple[str, Optional[str]]:
    """Map an app-server approval request into PermissionManager fields."""
    normalized_method = (method or "").strip()
    safe_params = params if isinstance(params, dict) else {}

    if normalized_method == "item/commandExecution/requestApproval":
        command = safe_params.get("command")
        reason = safe_params.get("reason")
        cwd = safe_params.get("cwd")
        input_lines = []
        if command:
            input_lines.append(f"command: {command}")
        if cwd:
            input_lines.append(f"cwd: {cwd}")
        if reason:
            input_lines.append(f"reason: {reason}")
        return "run_command", "\n".join(input_lines) if input_lines else None

    if normalized_method == "item/fileChange/requestApproval":
        reason = safe_params.get("reason")
        grant_root = safe_params.get("grantRoot")
        input_lines = []
        if reason:
            input_lines.append(f"reason: {reason}")
        if grant_root:
            input_lines.append(f"grantRoot: {grant_root}")
        return "file_change", "\n".join(input_lines) if input_lines else None

    if normalized_method == "skill/requestApproval":
        skill_name = str(safe_params.get("skillName") or "unknown")
        return f"skill:{skill_name}", None

    return normalized_method or "codex_approval", json.dumps(safe_params, default=str)


def approval_payload_from_decision(method: str, approved: bool) -> dict:
    """Convert a boolean Slack decision into app-server approval payload."""
    normalized_method = (method or "").strip()

    if normalized_method == "skill/requestApproval":
        return {"decision": "approve" if approved else "decline"}

    if normalized_method in {"execCommandApproval", "applyPatchApproval"}:
        return {"decision": "approved" if approved else "denied"}

    return {"decision": "accept" if approved else "decline"}


def default_approval_payload(method: str, approval_mode: str) -> dict:
    """Return default app-server approval payload when no user decision is available."""
    should_accept = approval_mode == "never"
    return approval_payload_from_decision(method, should_accept)
