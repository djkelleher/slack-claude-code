"""Unit tests for backend-aware command routing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import config
from src.database.models import Session, WorkspaceLease
from src.git.workspace_manager import PreparedWorkspace
from src.handlers.command_router import (
    _build_claude_plan_prompt,
    execute_for_session,
    resolve_backend_for_session,
)
from src.utils.mode_directives import PlanModeDirective


class TestCommandRouter:
    """Tests for route selection and execution."""

    def test_build_claude_plan_prompt_creates_plans_dir(self, tmp_path, monkeypatch):
        """Claude plan prompt should ensure the plans directory exists."""
        plans_dir = tmp_path / "plans"
        monkeypatch.setattr("src.handlers.command_router.PLANS_DIR", str(plans_dir))

        prompt = _build_claude_plan_prompt(
            "Plan this change",
            session_id=42,
            execution_id="exec-123",
        )

        assert plans_dir.is_dir()
        expected_path = Path(plans_dir) / "plan-session-42-exec-123.md"
        assert str(expected_path) in prompt

    def test_resolve_backend_for_session(self):
        """Backend resolution follows selected model."""
        assert resolve_backend_for_session(Session(model="opus")) == "claude"
        assert resolve_backend_for_session(Session(model="gpt-5.3-codex")) == "codex"

    @pytest.mark.asyncio
    async def test_execute_for_session_claude(self):
        """Claude sessions call Claude executor and persist Claude session ID."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.executor.execute.return_value = SimpleNamespace(
            session_id="claude-new", success=True
        )

        session = Session(
            id=7,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts=None,
            execution_id="exec-1",
        )

        assert routed.backend == "claude"
        deps.executor.execute.assert_awaited_once()
        deps.codex_executor.execute.assert_not_called()
        deps.db.update_session_claude_id.assert_awaited_once_with(
            "C123", None, "claude-new"
        )
        deps.db.update_session_codex_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_codex(self):
        """Codex sessions call Codex executor and persist Codex session ID."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output="",
        )

        session = Session(
            id=9,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-2",
        )

        assert routed.backend == "codex"
        deps.codex_executor.execute.assert_awaited_once()
        deps.executor.execute.assert_not_called()
        deps.db.update_session_codex_id.assert_awaited_once_with(
            "C123", "123.4", "codex-new"
        )
        deps.db.update_session_claude_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_uses_prepared_auto_worktree_and_skips_persistence(
        self,
    ):
        """Auto-worktree executions should run in the leased cwd without persisting IDs."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_active_workspace_lease_by_root=AsyncMock(return_value=None),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.executor.execute.return_value = SimpleNamespace(
            session_id="claude-auto",
            success=True,
            output="done",
            detailed_output="",
        )

        session = Session(
            id=21,
            channel_id="C123",
            model="opus",
            working_directory="/repo",
            claude_session_id="claude-old",
        )
        prepared = PreparedWorkspace(
            lease=WorkspaceLease(
                session_id=21,
                channel_id="C123",
                session_scope="C123",
                execution_id="exec-auto",
                repo_root="/repo",
                target_worktree_path="/repo",
                target_branch="main",
                leased_root="/repo-worktrees/auto",
                leased_cwd="/repo-worktrees/auto",
                base_cwd="/repo",
                lease_kind="worktree",
                worktree_name="slack-auto/exec-auto",
                status="active",
            ),
            session=Session(
                id=21,
                channel_id="C123",
                model="opus",
                working_directory="/repo-worktrees/auto",
            ),
            persist_session_ids=False,
        )

        with (
            patch(
                "src.handlers.command_router.WorkspaceManager.prepare_workspace",
                new=AsyncMock(return_value=prepared),
            ),
            patch(
                "src.handlers.command_router.WorkspaceManager.release_workspace",
                new=AsyncMock(),
            ) as mock_release,
            patch(
                "src.handlers.command_router._reintegrate_auto_worktree",
                new=AsyncMock(
                    return_value=(
                        SimpleNamespace(
                            session_id="claude-auto",
                            success=True,
                            output="done",
                            detailed_output="",
                        ),
                        "merged",
                        ["Merged successfully."],
                    )
                ),
            ),
        ):
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="hello",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-auto",
            )

        assert routed.backend == "claude"
        assert (
            deps.executor.execute.await_args.kwargs["working_directory"]
            == "/repo-worktrees/auto"
        )
        deps.db.update_session_claude_id.assert_not_called()
        mock_release.assert_awaited_once_with(
            "exec-auto",
            status="merged",
            merge_status="merged",
        )

    @pytest.mark.asyncio
    async def test_execute_for_session_claude_skips_id_persistence_when_disabled(self):
        """When persist_session_ids=False, Claude session IDs should not be written to DB."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.executor.execute.return_value = SimpleNamespace(
            session_id="claude-new", success=True
        )

        session = Session(
            id=7, model="opus", working_directory="/tmp", claude_session_id="claude-old"
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts=None,
            execution_id="exec-2a",
            persist_session_ids=False,
        )

        assert routed.backend == "claude"
        deps.db.update_session_claude_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_skips_id_persistence_when_disabled(self):
        """When persist_session_ids=False, Codex session IDs should not be written to DB."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output="",
        )

        session = Session(
            id=9,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-2b",
            persist_session_ids=False,
        )

        assert routed.backend == "codex"
        deps.db.update_session_codex_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_without_executor(self):
        """Codex routing fails fast when no Codex executor is configured."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=None,
        )
        session = Session(id=1, model="gpt-5.3-codex", working_directory="/tmp")

        with pytest.raises(RuntimeError, match="Codex executor is not configured"):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="hello",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-3",
            )

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_plan_mode_passes_permission_mode(self):
        """Codex plan mode augments prompt format guidance and forwards permission mode."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output="",
        )

        session = Session(
            id=11,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="Implement feature",
            channel_id="C123",
            thread_ts=None,
            execution_id="exec-4",
        )

        kwargs = deps.codex_executor.execute.await_args.kwargs
        assert "Implement feature" in kwargs["prompt"]
        assert "PLAN_STATUS: READY" in kwargs["prompt"]
        assert kwargs["permission_mode"] == "plan"

    @pytest.mark.asyncio
    async def test_codex_plan_mode_splan_uses_adversarial_flow_and_planner_model(self):
        """`splan` should request approval with summary and continue using planner model."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )
        deps.codex_executor.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(session_id="codex-1", success=True, output="initial"),
                SimpleNamespace(
                    session_id="codex-1",
                    success=True,
                    output=(
                        "PLAN_STATUS: READY\n"
                        "# Implementation Plan\n"
                        "## Steps\n"
                        "- Draft\n- Validate\n- Ship\n"
                        "## Risks\n- Scope\n"
                        "## Test Plan\n- Unit\n"
                    ),
                ),
                SimpleNamespace(
                    session_id="codex-1",
                    success=True,
                    output=(
                        "PLAN_STATUS: READY\n"
                        "# Implementation Plan\n"
                        "## Steps\n"
                        "- Revise draft\n- Validate changes\n- Ship\n"
                        "## Risks\n- Scope\n"
                        "## Test Plan\n- Unit\n"
                    ),
                ),
                SimpleNamespace(
                    session_id="codex-1", success=True, output="implemented"
                ),
            ]
        )

        session = Session(
            id=20,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        plan_directive = PlanModeDirective(
            strategy="splan",
            models=("gpt-5.4-high", "gpt-5.3-codex"),
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ) as mock_request_approval:
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-splan-1",
                slack_client=SimpleNamespace(),
                user_id="U123",
                plan_mode_directive=plan_directive,
            )

        assert routed.result.success is True
        assert deps.codex_executor.execute.await_count == 4
        approval_kwargs = mock_request_approval.await_args.kwargs
        assert "Adversarial Planning Summary" in approval_kwargs["plan_content"]
        assert "`splan`" in approval_kwargs["plan_content"]
        final_call_kwargs = deps.codex_executor.execute.await_args.kwargs
        assert final_call_kwargs["model"] == "gpt-5.4-high"
        assert final_call_kwargs["permission_mode"] == config.DEFAULT_BYPASS_MODE

    @pytest.mark.asyncio
    async def test_codex_plan_mode_splan_rejects_non_codex_planner_model(self):
        """`splan` planner must match active backend."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(
                        session_id="codex-1",
                        success=True,
                        output="initial",
                    )
                )
            ),
        )
        session = Session(
            id=21,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        plan_directive = PlanModeDirective(
            strategy="splan",
            models=("claude-sonnet-4-6-high", "gpt-5.4-high"),
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ) as mock_request_approval:
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-splan-2",
                slack_client=SimpleNamespace(),
                user_id="U123",
                plan_mode_directive=plan_directive,
            )

        assert routed.result.success is False
        assert "planner configuration" in (routed.result.output or "")
        mock_request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_plan_mode_skips_approval_for_non_plan_output(self):
        """Plan mode should not request approval for generic clarification text."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output=(
                "Ready to help. Share the change you want, and I will provide a concrete "
                "implementation plan first, then wait for your confirmation."
            ),
        )

        session = Session(
            id=12,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ) as mock_request_approval:
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="hi",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-5",
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        assert deps.codex_executor.execute.await_count == 2
        mock_request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_plan_mode_retries_with_canonical_format_and_requests_approval(
        self,
    ):
        """Non-detected plan responses should trigger one canonical-format retry."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        first_response = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output=(
                "I'm ready to implement it end-to-end, but I'm currently locked in Plan Mode. "
                "Switch to default to proceed."
            ),
        )
        second_response = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output=(
                "PLAN_STATUS: READY\n"
                "# Implementation Plan\n"
                "## Steps\n"
                "- Gather requirements\n"
                "- Implement changes\n"
                "- Validate behavior\n"
                "## Risks\n"
                "- Scope drift\n"
                "## Test Plan\n"
                "- Run unit tests\n"
            ),
        )
        deps.codex_executor.execute = AsyncMock(
            side_effect=[first_response, second_response]
        )

        session = Session(
            id=16,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=False),
        ) as mock_request_approval:
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-5b",
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        assert deps.codex_executor.execute.await_count == 2
        mock_request_approval.assert_awaited_once()
        approval_kwargs = mock_request_approval.await_args.kwargs
        assert approval_kwargs["plan_content"].startswith("PLAN_STATUS: READY")
        assert (
            routed.result.output
            == "_Plan not approved. Staying in plan mode until you provide feedback._"
        )
        assert "PLAN_STATUS: READY" not in routed.result.output

    @pytest.mark.asyncio
    async def test_codex_plan_mode_namespaces_tool_activity_ids_per_turn(self):
        """Post-approval execution tool activity IDs should not collide with plan turn IDs."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        call_index = 0

        async def _fake_codex_execute(**kwargs):
            nonlocal call_index
            call_index += 1
            on_chunk = kwargs["on_chunk"]

            await on_chunk(
                SimpleNamespace(
                    type="tool_call",
                    content="",
                    tool_activities=[SimpleNamespace(id="item_1")],
                )
            )

            if call_index == 1:
                await on_chunk(
                    SimpleNamespace(
                        type="assistant",
                        content=(
                            "# Implementation Plan\n"
                            "1. Add streaming tool ID namespacing for multi-turn Codex flows.\n"
                            "2. Preserve tool activity visibility after plan approval.\n"
                            "3. Add regression tests for turn separation.\n\n"
                            "## Test Plan\n"
                            "- Run command router unit tests.\n"
                        ),
                        tool_activities=[],
                    )
                )
                return SimpleNamespace(
                    session_id="codex-new",
                    success=True,
                    output=(
                        "# Implementation Plan\n"
                        "1. Add streaming tool ID namespacing for multi-turn Codex flows.\n"
                        "2. Preserve tool activity visibility after plan approval.\n"
                        "3. Add regression tests for turn separation.\n\n"
                        "## Test Plan\n"
                        "- Run command router unit tests.\n"
                    ),
                )

            await on_chunk(
                SimpleNamespace(
                    type="assistant",
                    content="Implementation complete.",
                    tool_activities=[],
                )
            )
            return SimpleNamespace(
                session_id="codex-new",
                success=True,
                output="Implementation complete.",
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)

        session = Session(
            id=13,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        on_chunk = AsyncMock()
        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-6",
                on_chunk=on_chunk,
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        assert deps.codex_executor.execute.await_count == 2
        tool_ids: list[str] = []
        for call in on_chunk.await_args_list:
            msg = call.args[0]
            for tool in msg.tool_activities:
                tool_ids.append(tool.id)

        assert "turn1:item_1" in tool_ids
        assert "turn2:item_1" in tool_ids

    @pytest.mark.asyncio
    async def test_codex_plan_mode_can_swap_streaming_callback_after_approval(self):
        """Approval hook should be able to move post-approval streaming to a new target."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        initial_on_chunk = AsyncMock()
        replacement_on_chunk = AsyncMock()
        call_index = 0

        async def _fake_codex_execute(**kwargs):
            nonlocal call_index
            call_index += 1
            on_chunk = kwargs["on_chunk"]
            await on_chunk(
                SimpleNamespace(
                    type="assistant",
                    content="",
                    tool_activities=[SimpleNamespace(id="item_1")],
                )
            )
            if call_index == 1:
                return SimpleNamespace(
                    session_id="codex-new",
                    success=True,
                    output=(
                        "# Implementation Plan\n"
                        "1. Add a new streaming message after plan approval.\n"
                        "2. Route post-approval tool activity to that message.\n"
                        "3. Verify with a regression test.\n\n"
                        "## Test Plan\n"
                        "- Run command router unit tests.\n"
                    ),
                )
            return SimpleNamespace(
                session_id="codex-new",
                success=True,
                output="Implementation complete.",
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)

        session = Session(
            id=15,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        on_plan_approved = AsyncMock(return_value=replacement_on_chunk)

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-6b",
                on_chunk=initial_on_chunk,
                slack_client=SimpleNamespace(),
                user_id="U123",
                on_plan_approved=on_plan_approved,
            )

        on_plan_approved.assert_awaited_once()
        assert initial_on_chunk.await_count == 1
        assert replacement_on_chunk.await_count == 1

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_thread_forks_inherited_channel_thread(
        self,
    ):
        """Thread-scoped Codex sessions should fork inherited channel thread IDs."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id="codex-shared")
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(
                        session_id="codex-forked", success=True, output=""
                    )
                ),
                thread_fork=AsyncMock(return_value={"thread": {"id": "codex-forked"}}),
            ),
        )

        session = Session(
            id=14,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-shared",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-7",
        )

        deps.codex_executor.thread_fork.assert_awaited_once_with(
            thread_id="codex-shared",
            working_directory="/tmp",
        )
        assert (
            deps.codex_executor.execute.await_args.kwargs["resume_session_id"]
            == "codex-forked"
        )
        assert deps.db.update_session_codex_id.await_args_list[0].args == (
            "C123",
            "123.4",
            "codex-forked",
        )
        assert session.codex_session_id == "codex-forked"

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_thread_fork_failure_uses_inherited_thread(
        self,
    ):
        """Fork failures should not block execution for thread-scoped Codex sessions."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id="codex-shared")
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(
                        session_id="codex-shared", success=True, output=""
                    )
                ),
                thread_fork=AsyncMock(side_effect=RuntimeError("fork unavailable")),
            ),
        )

        session = Session(
            id=15,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-shared",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        logger = SimpleNamespace(warning=MagicMock(), info=MagicMock())

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-8",
            logger=logger,
        )

        assert (
            deps.codex_executor.execute.await_args.kwargs["resume_session_id"]
            == "codex-shared"
        )

    @pytest.mark.asyncio
    async def test_codex_question_limit_does_not_fail_on_exact_limit(self):
        """Hitting exactly the question limit should not force a failed result."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            return SimpleNamespace(
                session_id="codex-new", success=True, output="Implementation complete."
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=16,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq1", tool_use_id="item_1")

        with patch.object(
            config.timeouts.execution, "max_questions_per_conversation", 1
        ):
            with patch(
                "src.handlers.command_router.QuestionManager.create_pending_question",
                new=AsyncMock(return_value=pending_question),
            ):
                with patch(
                    "src.handlers.command_router.QuestionManager.post_question_to_slack",
                    new=AsyncMock(),
                ):
                    with patch(
                        "src.handlers.command_router.QuestionManager.wait_for_answer",
                        new=AsyncMock(return_value={0: ["Yes"]}),
                    ):
                        with patch(
                            "src.handlers.command_router.QuestionManager.format_answer",
                            return_value={"answers": {"q_1": {"answers": ["Yes"]}}},
                        ):
                            routed = await execute_for_session(
                                deps=deps,
                                session=session,
                                prompt="hello",
                                channel_id="C123",
                                thread_ts=None,
                                execution_id="exec-9",
                                slack_client=SimpleNamespace(),
                                user_id="U123",
                            )

        assert routed.result.success is True
        assert routed.result.output == "Implementation complete."

    @pytest.mark.asyncio
    async def test_codex_auto_answers_recommended_queue_questions(self):
        """Auto-answer mode should bypass Slack question UI for Codex prompts."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [
                                {"label": "No", "description": "Stop"},
                                {
                                    "label": "Yes (Recommended)",
                                    "description": "Continue",
                                },
                            ],
                        }
                    ]
                },
            )
            assert payload == {"answers": {"q_1": {"answers": ["Yes (Recommended)"]}}}
            return SimpleNamespace(session_id="codex-new", success=True, output="Done.")

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=20,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.QuestionManager.create_pending_question",
            new=AsyncMock(),
        ) as create_pending_question:
            with patch(
                "src.handlers.command_router.QuestionManager.post_question_to_slack",
                new=AsyncMock(),
            ) as post_question_to_slack:
                with patch(
                    "src.handlers.command_router.QuestionManager.wait_for_answer",
                    new=AsyncMock(),
                ) as wait_for_answer:
                    routed = await execute_for_session(
                        deps=deps,
                        session=session,
                        prompt="hello",
                        channel_id="C123",
                        thread_ts=None,
                        execution_id="exec-9-auto",
                        slack_client=SimpleNamespace(),
                        user_id="U123",
                        auto_answer_questions=True,
                    )

        create_pending_question.assert_not_awaited()
        post_question_to_slack.assert_not_awaited()
        wait_for_answer.assert_not_awaited()
        assert routed.result.success is True
        assert routed.result.output == "Done."

    @pytest.mark.asyncio
    async def test_codex_pause_on_questions_posts_question_and_returns_pause_signal(
        self,
    ):
        """Pause-on-question mode should post Slack question UI and end turn for queue pause."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            try:
                await kwargs["on_user_input_request"](
                    "item_1",
                    {
                        "questions": [
                            {
                                "id": "q_1",
                                "question": "Proceed?",
                                "header": "Confirm",
                                "options": [
                                    {"label": "Yes", "description": "Continue"}
                                ],
                            }
                        ]
                    },
                )
            except RuntimeError as pause_error:
                return SimpleNamespace(
                    session_id="codex-new",
                    success=False,
                    output="",
                    error=str(pause_error),
                )

            return SimpleNamespace(session_id="codex-new", success=True, output="Done.")

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=24,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq-codex", tool_use_id="item_1")

        with patch(
            "src.handlers.command_router.QuestionManager.create_pending_question",
            new=AsyncMock(return_value=pending_question),
        ) as create_pending_question:
            with patch(
                "src.handlers.command_router.QuestionManager.post_question_to_slack",
                new=AsyncMock(),
            ) as post_question_to_slack:
                with patch(
                    "src.handlers.command_router.QuestionManager.wait_for_answer",
                    new=AsyncMock(),
                ) as wait_for_answer:
                    routed = await execute_for_session(
                        deps=deps,
                        session=session,
                        prompt="hello",
                        channel_id="C123",
                        thread_ts=None,
                        execution_id="exec-9-pause-codex",
                        slack_client=SimpleNamespace(),
                        user_id="U123",
                        pause_on_questions=True,
                    )

        create_pending_question.assert_awaited_once()
        post_question_to_slack.assert_awaited_once()
        wait_for_answer.assert_not_awaited()
        assert routed.result.success is False
        assert routed.result.paused_on_question is True
        assert routed.result.error == "__QUEUE_PAUSE_ON_QUESTION__"

    @pytest.mark.asyncio
    async def test_codex_pause_on_questions_replays_deferred_answer(self):
        """Deferred pause-resume answers should be replayed instead of pausing again."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            return SimpleNamespace(
                session_id="codex-new",
                success=True,
                output="Done.",
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=124,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.QuestionManager.consume_deferred_answer",
            new=AsyncMock(return_value={0: ["Yes"]}),
        ) as consume_deferred_answer:
            with patch(
                "src.handlers.command_router.QuestionManager.post_question_to_slack",
                new=AsyncMock(),
            ) as post_question_to_slack:
                routed = await execute_for_session(
                    deps=deps,
                    session=session,
                    prompt="hello",
                    channel_id="C123",
                    thread_ts=None,
                    execution_id="exec-9-pause-codex-deferred",
                    slack_client=SimpleNamespace(),
                    user_id="U123",
                    pause_on_questions=True,
                )

        consume_deferred_answer.assert_awaited_once()
        post_question_to_slack.assert_not_awaited()
        assert routed.result.success is True
        assert getattr(routed.result, "paused_on_question", False) is False

    @pytest.mark.asyncio
    async def test_codex_auto_approves_permissions_for_queue_execution(self):
        """Auto-approve mode should bypass Slack permission UI for Codex prompts."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            payload = await kwargs["on_approval_request"](
                "item/commandExecution/requestApproval",
                {"command": "ls"},
            )
            assert payload == {"decision": "accept"}
            return SimpleNamespace(session_id="codex-new", success=True, output="Done.")

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=22,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.PermissionManager.request_approval",
            new=AsyncMock(),
        ) as request_approval:
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="hello",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-9-auto-approval",
                slack_client=SimpleNamespace(),
                user_id="U123",
                auto_approve_permissions=True,
            )

        request_approval.assert_not_awaited()
        assert routed.result.success is True
        assert routed.result.output == "Done."

    @pytest.mark.asyncio
    async def test_codex_question_resume_can_swap_streaming_callback(self):
        """Question answers should allow Codex streaming to move to a new Slack message."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        initial_on_chunk = AsyncMock()
        replacement_on_chunk = AsyncMock()

        async def _fake_codex_execute(**kwargs):
            await kwargs["on_chunk"](
                SimpleNamespace(
                    type="assistant", content="First turn.", tool_activities=[]
                )
            )
            payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            await kwargs["on_chunk"](
                SimpleNamespace(
                    type="assistant", content="After answer.", tool_activities=[]
                )
            )
            return SimpleNamespace(
                session_id="codex-new", success=True, output="After answer."
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=18,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq2", tool_use_id="item_1")
        on_interaction_resumed = AsyncMock(return_value=replacement_on_chunk)

        with patch(
            "src.handlers.command_router.QuestionManager.create_pending_question",
            new=AsyncMock(return_value=pending_question),
        ):
            with patch(
                "src.handlers.command_router.QuestionManager.post_question_to_slack",
                new=AsyncMock(),
            ):
                with patch(
                    "src.handlers.command_router.QuestionManager.wait_for_answer",
                    new=AsyncMock(return_value={0: ["Yes"]}),
                ):
                    with patch(
                        "src.handlers.command_router.QuestionManager.format_answer",
                        return_value={"answers": {"q_1": {"answers": ["Yes"]}}},
                    ):
                        await execute_for_session(
                            deps=deps,
                            session=session,
                            prompt="hello",
                            channel_id="C123",
                            thread_ts=None,
                            execution_id="exec-9b",
                            on_chunk=initial_on_chunk,
                            slack_client=SimpleNamespace(),
                            user_id="U123",
                            on_interaction_resumed=on_interaction_resumed,
                        )

        on_interaction_resumed.assert_awaited_once()
        assert initial_on_chunk.await_count == 1
        assert replacement_on_chunk.await_count == 1

    @pytest.mark.asyncio
    async def test_claude_auto_answers_recommended_queue_questions(self):
        """Auto-answer mode should resume Claude without waiting for Slack input."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )

        prompts: list[str] = []

        async def _fake_claude_execute(**kwargs):
            prompts.append(kwargs["prompt"])
            on_chunk = kwargs["on_chunk"]
            if len(prompts) == 1:
                await on_chunk(
                    SimpleNamespace(
                        type="assistant",
                        content="",
                        tool_activities=[
                            SimpleNamespace(
                                name="AskUserQuestion",
                                result=None,
                                id="tool_1",
                                input={
                                    "questions": [
                                        {
                                            "id": "q_1",
                                            "question": "How should we proceed?",
                                            "header": "Decision",
                                            "options": [
                                                {"label": "Do nothing"},
                                                {
                                                    "label": "Use fast path (Recommended)"
                                                },
                                            ],
                                            "multiSelect": False,
                                        }
                                    ]
                                },
                            )
                        ],
                    )
                )
                return SimpleNamespace(
                    session_id="claude-new",
                    success=True,
                    output="Need input",
                    has_pending_question=True,
                    has_pending_plan_approval=False,
                )

            return SimpleNamespace(
                session_id="claude-new",
                success=True,
                output="Done.",
                has_pending_question=False,
                has_pending_plan_approval=False,
            )

        deps.executor.execute = AsyncMock(side_effect=_fake_claude_execute)
        session = Session(
            id=21,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
        )

        with patch(
            "src.handlers.command_router.QuestionManager.post_question_to_slack",
            new=AsyncMock(),
        ) as post_question_to_slack:
            with patch(
                "src.handlers.command_router.QuestionManager.wait_for_answer",
                new=AsyncMock(),
            ) as wait_for_answer:
                routed = await execute_for_session(
                    deps=deps,
                    session=session,
                    prompt="hello",
                    channel_id="C123",
                    thread_ts=None,
                    execution_id="exec-9-auto-claude",
                    slack_client=SimpleNamespace(),
                    auto_answer_questions=True,
                )

        post_question_to_slack.assert_not_awaited()
        wait_for_answer.assert_not_awaited()
        assert len(prompts) == 2
        assert prompts[1] == "Use fast path (Recommended)"
        assert routed.result.success is True
        assert routed.result.output == "Done."

    @pytest.mark.asyncio
    async def test_claude_pause_on_questions_posts_question_without_waiting_for_answer(
        self,
    ):
        """Pause-on-question mode should post question UI and return pause signal immediately."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )

        async def _fake_claude_execute(**kwargs):
            await kwargs["on_chunk"](
                SimpleNamespace(
                    type="assistant",
                    content="Need input.",
                    tool_activities=[
                        SimpleNamespace(
                            name="AskUserQuestion",
                            result=None,
                            id="tool_1",
                            input={
                                "questions": [
                                    {
                                        "id": "q_1",
                                        "question": "How should we proceed?",
                                        "header": "Decision",
                                        "options": [
                                            {"label": "Do nothing"},
                                            {"label": "Use fast path"},
                                        ],
                                        "multiSelect": False,
                                    }
                                ]
                            },
                        )
                    ],
                )
            )
            return SimpleNamespace(
                session_id="claude-new",
                success=True,
                output="Need input",
                has_pending_question=True,
                has_pending_plan_approval=False,
            )

        deps.executor.execute = AsyncMock(side_effect=_fake_claude_execute)
        session = Session(
            id=25,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
        )
        pending_question = SimpleNamespace(
            question_id="pq-claude",
            tool_use_id="tool_1",
            questions=[
                SimpleNamespace(
                    question="How should we proceed?",
                    header="Decision",
                    options=[
                        SimpleNamespace(label="Do nothing"),
                        SimpleNamespace(label="Use fast path"),
                    ],
                )
            ],
            answers={},
        )

        with patch(
            "src.handlers.command_router.QuestionManager.create_pending_question",
            new=AsyncMock(return_value=pending_question),
        ) as create_pending_question:
            with patch(
                "src.handlers.command_router.QuestionManager.post_question_to_slack",
                new=AsyncMock(),
            ) as post_question_to_slack:
                with patch(
                    "src.handlers.command_router.QuestionManager.wait_for_answer",
                    new=AsyncMock(),
                ) as wait_for_answer:
                    routed = await execute_for_session(
                        deps=deps,
                        session=session,
                        prompt="hello",
                        channel_id="C123",
                        thread_ts=None,
                        execution_id="exec-9-pause-claude",
                        slack_client=SimpleNamespace(),
                        user_id="U123",
                        pause_on_questions=True,
                    )

        create_pending_question.assert_awaited_once()
        post_question_to_slack.assert_awaited_once()
        wait_for_answer.assert_not_awaited()
        assert routed.result.success is False
        assert routed.result.paused_on_question is True
        assert routed.result.error == "__QUEUE_PAUSE_ON_QUESTION__"

    @pytest.mark.asyncio
    async def test_claude_pause_on_questions_replays_deferred_answer(self):
        """Deferred pause-resume answers should let Claude continue without re-pausing."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        prompts: list[str] = []

        async def _fake_claude_execute(**kwargs):
            prompts.append(kwargs["prompt"])
            if len(prompts) == 1:
                await kwargs["on_chunk"](
                    SimpleNamespace(
                        type="assistant",
                        content="Need input.",
                        tool_activities=[
                            SimpleNamespace(
                                name="AskUserQuestion",
                                result=None,
                                id="tool_1",
                                input={
                                    "questions": [
                                        {
                                            "id": "q_1",
                                            "question": "How should we proceed?",
                                            "header": "Decision",
                                            "options": [
                                                {"label": "Do nothing"},
                                                {"label": "Use fast path"},
                                            ],
                                            "multiSelect": False,
                                        }
                                    ]
                                },
                            )
                        ],
                    )
                )
                return SimpleNamespace(
                    session_id="claude-new",
                    success=True,
                    output="Need input",
                    has_pending_question=True,
                    has_pending_plan_approval=False,
                )
            return SimpleNamespace(
                session_id="claude-new",
                success=True,
                output="Done.",
                has_pending_question=False,
                has_pending_plan_approval=False,
            )

        deps.executor.execute = AsyncMock(side_effect=_fake_claude_execute)
        session = Session(
            id=125,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
        )
        pending_question = SimpleNamespace(
            question_id="pq-claude",
            tool_use_id="tool_1",
            questions=[
                SimpleNamespace(
                    question="How should we proceed?",
                    header="Decision",
                    options=[
                        SimpleNamespace(label="Do nothing"),
                        SimpleNamespace(label="Use fast path"),
                    ],
                )
            ],
            answers={},
        )

        with patch(
            "src.handlers.command_router.QuestionManager.create_pending_question",
            new=AsyncMock(return_value=pending_question),
        ) as create_pending_question:
            with patch(
                "src.handlers.command_router.QuestionManager.consume_deferred_answer",
                new=AsyncMock(return_value={0: ["Use fast path"]}),
            ) as consume_deferred_answer:
                with patch(
                    "src.handlers.command_router.QuestionManager.post_question_to_slack",
                    new=AsyncMock(),
                ) as post_question_to_slack:
                    routed = await execute_for_session(
                        deps=deps,
                        session=session,
                        prompt="hello",
                        channel_id="C123",
                        thread_ts=None,
                        execution_id="exec-9-pause-claude-deferred",
                        slack_client=SimpleNamespace(),
                        user_id="U123",
                        pause_on_questions=True,
                    )

        create_pending_question.assert_awaited_once()
        consume_deferred_answer.assert_awaited_once()
        post_question_to_slack.assert_not_awaited()
        assert prompts == ["hello", "Use fast path"]
        assert routed.result.success is True
        assert getattr(routed.result, "paused_on_question", False) is False

    @pytest.mark.asyncio
    async def test_claude_plan_rejection_does_not_replay_plan_output(self):
        """Rejected Claude plans should return a concise status without duplicating plan text."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id=None)
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(), thread_fork=AsyncMock()
            ),
        )
        deps.executor.execute.return_value = SimpleNamespace(
            session_id="claude-new",
            success=True,
            output=(
                "# Implementation Plan\n"
                "1. Scope work.\n"
                "2. Implement update.\n"
                "3. Run tests.\n\n"
                "## Test Plan\n"
                "- pytest\n"
            ),
            has_pending_question=False,
            has_pending_plan_approval=True,
            plan_subagent_result="",
        )

        session = Session(
            id=23,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
            permission_mode="plan",
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=False),
        ):
            routed = await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-claude-plan-reject",
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        assert routed.result.success is False
        assert (
            routed.result.output
            == "_Plan not approved. Staying in plan mode until you provide feedback._"
        )
        assert "# Implementation Plan" not in routed.result.output

    @pytest.mark.asyncio
    async def test_codex_approval_resume_can_swap_streaming_callback(self):
        """Approval decisions should allow Codex streaming to move to a new Slack message."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        initial_on_chunk = AsyncMock()
        replacement_on_chunk = AsyncMock()

        async def _fake_codex_execute(**kwargs):
            await kwargs["on_chunk"](
                SimpleNamespace(
                    type="assistant", content="Before approval.", tool_activities=[]
                )
            )
            payload = await kwargs["on_approval_request"](
                "permissions.request",
                {"tool_name": "shell", "input": {"command": "ls"}},
            )
            assert payload is not None
            await kwargs["on_chunk"](
                SimpleNamespace(
                    type="assistant", content="After approval.", tool_activities=[]
                )
            )
            return SimpleNamespace(
                session_id="codex-new", success=True, output="After approval."
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=19,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        on_interaction_resumed = AsyncMock(return_value=replacement_on_chunk)

        with patch(
            "src.handlers.command_router.PermissionManager.request_approval",
            new=AsyncMock(return_value=True),
        ):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="hello",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-9c",
                on_chunk=initial_on_chunk,
                slack_client=SimpleNamespace(),
                user_id="U123",
                on_interaction_resumed=on_interaction_resumed,
            )

        on_interaction_resumed.assert_awaited_once()
        assert initial_on_chunk.await_count == 1
        assert replacement_on_chunk.await_count == 1

    @pytest.mark.asyncio
    async def test_codex_question_limit_still_fails_when_extra_question_requested(self):
        """A question request beyond the limit should still mark the result as failed."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            first_payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert first_payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            second_payload = await kwargs["on_user_input_request"](
                "item_2",
                {
                    "questions": [
                        {
                            "id": "q_2",
                            "question": "Need another input?",
                            "header": "Confirm",
                            "options": [{"label": "No", "description": "Skip"}],
                        }
                    ]
                },
            )
            assert second_payload is None
            return SimpleNamespace(
                session_id="codex-new",
                success=True,
                output="Should not be final success.",
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=17,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq1", tool_use_id="item_1")

        with patch.object(
            config.timeouts.execution, "max_questions_per_conversation", 1
        ):
            with patch(
                "src.handlers.command_router.QuestionManager.create_pending_question",
                new=AsyncMock(return_value=pending_question),
            ):
                with patch(
                    "src.handlers.command_router.QuestionManager.post_question_to_slack",
                    new=AsyncMock(),
                ):
                    with patch(
                        "src.handlers.command_router.QuestionManager.wait_for_answer",
                        new=AsyncMock(return_value={0: ["Yes"]}),
                    ):
                        with patch(
                            "src.handlers.command_router.QuestionManager.format_answer",
                            return_value={"answers": {"q_1": {"answers": ["Yes"]}}},
                        ):
                            routed = await execute_for_session(
                                deps=deps,
                                session=session,
                                prompt="hello",
                                channel_id="C123",
                                thread_ts=None,
                                execution_id="exec-10",
                                slack_client=SimpleNamespace(),
                                user_id="U123",
                            )

        assert routed.result.success is False
        assert "Reached maximum question limit (1)." in routed.result.output
