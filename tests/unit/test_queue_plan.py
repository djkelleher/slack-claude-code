"""Unit tests for structured queue-plan parsing and materialization."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tasks.queue_plan import (
    QueuePlanError,
    contains_queue_plan_markers,
    materialize_queue_plan_prompts,
    materialize_queue_plan_text,
    parse_queue_plan_submission,
    parse_queue_plan_text,
)


def test_contains_queue_plan_markers_detects_known_markers() -> None:
    assert contains_queue_plan_markers("first\n***\nsecond") is True
    assert contains_queue_plan_markers("((append))\nfirst\n***\nsecond") is True
    assert contains_queue_plan_markers("((loop2))\nrun\n((endloop))") is True
    assert contains_queue_plan_markers("((branch feature/x))\nrun\n((endbranch))") is True
    assert contains_queue_plan_markers("((parallel2))\nrun\n***\nmore\n((endparallel))") is True


def test_contains_queue_plan_markers_ignores_non_marker_plain_text() -> None:
    assert contains_queue_plan_markers("normal prompt\nbold text\ncontinue") is False


def test_contains_queue_plan_markers_treats_invalid_markers_as_structured() -> None:
    assert contains_queue_plan_markers("***loop-0") is True
    assert contains_queue_plan_markers("***bold***") is True


def test_parse_queue_plan_separator_expands_prompts() -> None:
    prompts = parse_queue_plan_text("first task\n***\nsecond task")
    assert [item.prompt for item in prompts] == ["first task", "second task"]
    assert all(item.branch_name is None for item in prompts)


def test_parse_queue_plan_branch_section_scopes_prompts() -> None:
    prompts = parse_queue_plan_text(
        "((branch feature/auth))\ninside worktree\n***\nagain\n((endbranch))\noutside"
    )
    assert [item.prompt for item in prompts] == ["inside worktree", "again", "outside"]
    assert [item.branch_name for item in prompts] == [
        "feature/auth",
        "feature/auth",
        None,
    ]


def test_parse_queue_plan_branch_supports_single_line_statement() -> None:
    prompts = parse_queue_plan_text("((branch feature/auth)) run here")
    assert [item.prompt for item in prompts] == ["run here"]
    assert [item.branch_name for item in prompts] == ["feature/auth"]


def test_parse_queue_plan_loop_expands_prompts() -> None:
    prompts = parse_queue_plan_text("((loop3))\nrun once\n((endloop))")
    assert [item.prompt for item in prompts] == ["run once", "run once", "run once"]


def test_parse_queue_plan_loop_supports_single_line_statement() -> None:
    prompts = parse_queue_plan_text("((loop3)) continue")
    assert [item.prompt for item in prompts] == ["continue", "continue", "continue"]


def test_parse_queue_plan_parallel_block_assigns_shared_group() -> None:
    prompts = parse_queue_plan_text("((parallel2))\nfirst\n***\nsecond\n((endparallel))")
    assert [item.prompt for item in prompts] == ["first", "second"]
    assert prompts[0].parallel_group_id == prompts[1].parallel_group_id
    assert prompts[0].parallel_limit == 2
    assert prompts[1].parallel_limit == 2


def test_parse_queue_plan_parallel_inside_loop_creates_distinct_groups() -> None:
    prompts = parse_queue_plan_text("((loop2))\n((parallel))\none\n((endparallel))\n((endloop))")
    assert [item.prompt for item in prompts] == ["one", "one"]
    assert prompts[0].parallel_group_id != prompts[1].parallel_group_id
    assert prompts[0].parallel_limit is None
    assert prompts[1].parallel_limit is None


def test_parse_queue_plan_rejects_nested_parallel_blocks() -> None:
    with pytest.raises(QueuePlanError, match="nested parallel blocks"):
        parse_queue_plan_text("((parallel))\n((parallel2))\nrun\n((endparallel))\n((endparallel))")


def test_parse_queue_plan_allows_nested_loop_and_branch() -> None:
    prompts = parse_queue_plan_text(
        "((loop2))\n"
        "outside\n"
        "((branch feature/a))\n"
        "inside\n"
        "((endbranch))\n"
        "((endloop))"
    )
    assert [item.prompt for item in prompts] == [
        "outside",
        "inside",
        "outside",
        "inside",
    ]
    assert [item.branch_name for item in prompts] == [
        None,
        "feature/a",
        None,
        "feature/a",
    ]


def test_parse_queue_plan_allows_unclosed_loop_block_at_eof() -> None:
    prompts = parse_queue_plan_text("((loop2))\nrun")
    assert [item.prompt for item in prompts] == ["run", "run"]
    assert [item.branch_name for item in prompts] == [None, None]


def test_parse_queue_plan_allows_unclosed_branch_block_at_eof() -> None:
    prompts = parse_queue_plan_text("((branch feature/a))\ninside")
    assert [item.prompt for item in prompts] == ["inside"]
    assert [item.branch_name for item in prompts] == ["feature/a"]


def test_parse_queue_plan_rejects_branch_end_without_open_block() -> None:
    with pytest.raises(
        QueuePlanError, match="branch end marker without matching open branch block"
    ):
        parse_queue_plan_text("run\n((endbranch))")


def test_parse_queue_plan_reports_open_branch_when_loop_end_hits_branch_scope() -> None:
    with pytest.raises(QueuePlanError, match="currently inside branch `f2`"):
        parse_queue_plan_text("((loop2))\n((branch f2))\nrun\n((endloop))")


def test_parse_queue_plan_rejects_non_positive_loop_count() -> None:
    with pytest.raises(QueuePlanError, match="must be >= 1"):
        parse_queue_plan_text("((loop0))\nrun\n((endloop))")


def test_parse_queue_plan_rejects_legacy_loop_marker() -> None:
    with pytest.raises(QueuePlanError, match="Unknown queue-plan marker"):
        parse_queue_plan_text("first\n***loop-2\nsecond")


def test_parse_queue_plan_rejects_unknown_marker() -> None:
    with pytest.raises(QueuePlanError, match="Unknown queue-plan marker"):
        parse_queue_plan_text("first\n***\n***not-a-marker***\nsecond")


def test_parse_queue_plan_enforces_expansion_cap() -> None:
    with pytest.raises(QueuePlanError, match="more than 3 items"):
        parse_queue_plan_text("((loop4))\nrun\n((endloop))", max_expanded_items=3)


def test_parse_queue_plan_submission_defaults_to_append_pending() -> None:
    options, body = parse_queue_plan_submission("first task\n***\nsecond task")
    assert options.replace_pending is False
    assert options.directive_explicit is False
    assert body == "first task\n***\nsecond task"


def test_parse_queue_plan_submission_supports_append_directive() -> None:
    options, body = parse_queue_plan_submission("((append))\nfirst task\n***\nsecond task")
    assert options.replace_pending is False
    assert options.directive_explicit is True
    assert options.insertion_mode == "append"
    assert options.insert_at is None
    assert body == "first task\n***\nsecond task"


def test_parse_queue_plan_submission_supports_prepend_directive() -> None:
    options, body = parse_queue_plan_submission("((prepend))\nfirst task\n***\nsecond task")
    assert options.replace_pending is False
    assert options.directive_explicit is True
    assert options.insertion_mode == "prepend"
    assert options.insert_at == 1
    assert body == "first task\n***\nsecond task"


def test_parse_queue_plan_submission_supports_insert_directive() -> None:
    options, body = parse_queue_plan_submission("((insert2))\nfirst task\n***\nsecond task")
    assert options.replace_pending is False
    assert options.directive_explicit is True
    assert options.insertion_mode == "insert"
    assert options.insert_at == 2
    assert body == "first task\n***\nsecond task"


def test_parse_queue_plan_submission_rejects_clear_directive() -> None:
    with pytest.raises(QueuePlanError, match="handled by `/qc clear`"):
        parse_queue_plan_submission("((clear))\nfirst task")


def test_parse_queue_plan_submission_rejects_conflicting_directives() -> None:
    with pytest.raises(QueuePlanError, match="directives conflict"):
        parse_queue_plan_submission("((append))\n((prepend))\nfirst task")


def test_parse_queue_plan_submission_parses_timer_directives_with_iso_time() -> None:
    now = datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc)
    options, body = parse_queue_plan_submission(
        "((at 2026-03-13T18:30:00+00:00))\nfirst task",
        now_utc=now,
    )

    assert body == "first task"
    assert len(options.scheduled_controls) == 1
    assert options.scheduled_controls[0].action == "resume"
    assert options.scheduled_controls[0].execute_at == datetime(
        2026, 3, 13, 18, 30, tzinfo=timezone.utc
    )


def test_parse_queue_plan_submission_parses_timer_directives_with_hhmm_time() -> None:
    local_now = datetime.now().astimezone()
    now_utc = local_now.astimezone(timezone.utc)
    future_local = local_now + timedelta(minutes=5)
    hhmm = future_local.strftime("%H:%M")

    options, _ = parse_queue_plan_submission(f"((at {hhmm}))\nfirst task", now_utc=now_utc)

    assert len(options.scheduled_controls) == 1
    assert options.scheduled_controls[0].action == "resume"
    assert options.scheduled_controls[0].execute_at > now_utc


def test_parse_queue_plan_submission_rejects_past_timer_directive() -> None:
    now = datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc)
    with pytest.raises(QueuePlanError, match="in the past"):
        parse_queue_plan_submission(
            "((at 2026-03-13T17:59:00+00:00))\nfirst task",
            now_utc=now,
        )


def test_parse_queue_plan_submission_rejects_iso_time_without_timezone() -> None:
    now = datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc)
    with pytest.raises(QueuePlanError, match="must include a timezone offset"):
        parse_queue_plan_submission(
            "((at 2026-03-13T18:30:00))\nfirst task",
            now_utc=now,
        )


@pytest.mark.asyncio
async def test_materialize_queue_plan_without_branch_does_not_touch_git() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(),
        list_worktrees=AsyncMock(),
        add_worktree=AsyncMock(),
    )
    materialized = await materialize_queue_plan_text(
        text="first\n***\nsecond",
        working_directory="/repo",
        git_service=git_service,
    )

    assert [item.prompt for item in materialized] == ["first", "second"]
    assert all(item.working_directory_override is None for item in materialized)
    assert all(item.parallel_group_id is None for item in materialized)
    git_service.validate_git_repo.assert_not_called()
    git_service.list_worktrees.assert_not_called()
    git_service.add_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_queue_plan_resolves_existing_worktree() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(
            return_value=[
                SimpleNamespace(branch="feature/auth", path="/repo-worktrees/feature/auth")
            ]
        ),
        add_worktree=AsyncMock(),
    )
    materialized = await materialize_queue_plan_text(
        text="((branch feature/auth))\nrun\n((endbranch))",
        working_directory="/repo",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/auth"
    git_service.add_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_queue_plan_preserves_session_subdirectory_in_worktree() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(
            return_value=[
                SimpleNamespace(branch="main", path="/repo"),
                SimpleNamespace(branch="feature/auth", path="/repo-worktrees/feature/auth"),
            ]
        ),
        add_worktree=AsyncMock(),
    )

    materialized = await materialize_queue_plan_text(
        text="((branch feature/auth))\nrun\n((endbranch))",
        working_directory="/repo/services/api",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/auth/services/api"
    git_service.add_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_queue_plan_creates_missing_worktree() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(return_value=[]),
        add_worktree=AsyncMock(return_value="/repo-worktrees/feature/new"),
    )
    materialized = await materialize_queue_plan_text(
        text="((branch feature/new))\nrun\n((endbranch))",
        working_directory="/repo",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/new"
    git_service.add_worktree.assert_awaited_once_with("/repo", "feature/new", from_ref=None)


@pytest.mark.asyncio
async def test_materialize_queue_plan_rejects_branch_sections_outside_git_repo() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=False),
        list_worktrees=AsyncMock(),
        add_worktree=AsyncMock(),
    )
    with pytest.raises(QueuePlanError, match="not a git repository"):
        await materialize_queue_plan_text(
            text="((branch feature/new))\nrun\n((endbranch))",
            working_directory="/repo",
            git_service=git_service,
        )


@pytest.mark.asyncio
async def test_materialize_queue_plan_prompts_applies_branch_path_mapping() -> None:
    prompts = parse_queue_plan_text("((branch feature/a))\nfirst\n((endbranch))\nsecond")
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(return_value=[]),
        add_worktree=AsyncMock(return_value="/repo-worktrees/feature/a"),
    )

    materialized = await materialize_queue_plan_prompts(
        expanded=prompts,
        working_directory="/repo",
        git_service=git_service,
    )

    assert [item.working_directory_override for item in materialized] == [
        "/repo-worktrees/feature/a",
        None,
    ]


@pytest.mark.asyncio
async def test_materialize_queue_plan_preserves_subdirectory_for_new_worktree() -> None:
    prompts = parse_queue_plan_text("((branch feature/a))\nfirst\n((endbranch))")
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(return_value=[SimpleNamespace(branch="main", path="/repo")]),
        add_worktree=AsyncMock(return_value="/repo-worktrees/feature/a"),
    )

    materialized = await materialize_queue_plan_prompts(
        expanded=prompts,
        working_directory="/repo/services/api",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/a/services/api"


@pytest.mark.asyncio
async def test_materialize_queue_plan_preserves_parallel_metadata() -> None:
    materialized = await materialize_queue_plan_text(
        text="((parallel3))\nfirst\n***\nsecond\n((endparallel))",
        working_directory="/repo",
    )

    assert [item.parallel_limit for item in materialized] == [3, 3]
    assert materialized[0].parallel_group_id == materialized[1].parallel_group_id
