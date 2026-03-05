"""Structured queue-plan parser for prompt/worktree/loop DSL."""

import re
from dataclasses import dataclass
from typing import Optional

from src.git.service import GitError, GitService

MAX_EXPANDED_QUEUE_PLAN_ITEMS = 500

_ANY_MARKER_RE = re.compile(r"^\*\*\*.*\*\*\*$")
_BRANCH_START_RE = re.compile(r"^\*\*\*branch-(.+)\*\*\*$")
_BRANCH_END_RE = re.compile(r"^\*\*\*branch-(.+)-end\*\*\*$")
_LOOP_START_RE = re.compile(r"^\*\*\*loop-(-?\d+)\*\*\*$")
_LOOP_END_RE = re.compile(r"^\*\*\*loop-(-?\d+)-end\*\*\*$")


class QueuePlanError(ValueError):
    """Raised when structured queue-plan parsing or materialization fails."""


@dataclass(frozen=True)
class QueuePlanPrompt:
    """Expanded queue prompt with optional branch context."""

    prompt: str
    branch_name: Optional[str] = None


@dataclass(frozen=True)
class MaterializedQueuePlanPrompt:
    """Queue prompt ready for storage, with optional worktree override."""

    prompt: str
    working_directory_override: Optional[str] = None


@dataclass
class _PromptNode:
    prompt: str


@dataclass
class _BranchNode:
    branch_name: str
    children: list["_Node"]


@dataclass
class _LoopNode:
    count: int
    children: list["_Node"]


_Node = _PromptNode | _BranchNode | _LoopNode


@dataclass
class _Frame:
    kind: str
    start_line: int
    branch_name: Optional[str] = None
    loop_count: Optional[int] = None
    nodes: list[_Node] = None
    prompt_lines: list[str] = None

    def __post_init__(self) -> None:
        if self.nodes is None:
            self.nodes = []
        if self.prompt_lines is None:
            self.prompt_lines = []


def contains_queue_plan_markers(text: str) -> bool:
    """Return True when text includes at least one line-level queue-plan marker."""
    for line in text.splitlines():
        marker = _parse_marker(line.strip(), strict=False)
        if marker is not None:
            return True
    return False


def parse_queue_plan_text(
    text: str, max_expanded_items: int = MAX_EXPANDED_QUEUE_PLAN_ITEMS
) -> list[QueuePlanPrompt]:
    """Parse queue-plan DSL text into expanded prompt entries."""
    if max_expanded_items < 1:
        raise QueuePlanError("max_expanded_items must be at least 1")

    root = _parse_to_ast(text)
    expanded: list[QueuePlanPrompt] = []
    _expand_nodes(root, active_branch=None, out=expanded, max_items=max_expanded_items)

    if not expanded:
        raise QueuePlanError("No prompts found in structured queue plan.")
    return expanded


async def materialize_queue_plan_text(
    text: str,
    working_directory: str,
    git_service: Optional[GitService] = None,
    max_expanded_items: int = MAX_EXPANDED_QUEUE_PLAN_ITEMS,
) -> list[MaterializedQueuePlanPrompt]:
    """Parse + resolve queue-plan DSL into queue-ready prompt entries."""
    expanded = parse_queue_plan_text(text, max_expanded_items=max_expanded_items)
    return await materialize_queue_plan_prompts(
        expanded=expanded,
        working_directory=working_directory,
        git_service=git_service,
    )


async def materialize_queue_plan_prompts(
    expanded: list[QueuePlanPrompt],
    working_directory: str,
    git_service: Optional[GitService] = None,
) -> list[MaterializedQueuePlanPrompt]:
    """Resolve branch-scoped queue entries to concrete worktree paths."""
    branch_names = sorted({item.branch_name for item in expanded if item.branch_name})
    if not branch_names:
        return [MaterializedQueuePlanPrompt(prompt=item.prompt) for item in expanded]

    service = git_service or GitService()
    if not await service.validate_git_repo(working_directory):
        raise QueuePlanError(
            "Structured queue plan uses branch sections, but current working directory is not a "
            f"git repository: {working_directory}"
        )

    try:
        worktrees = await service.list_worktrees(working_directory)
    except GitError as e:
        raise QueuePlanError(f"Failed to list worktrees: {e}") from e

    worktree_paths_by_branch: dict[str, str] = {
        worktree.branch: worktree.path for worktree in worktrees if worktree.branch
    }

    for branch_name in branch_names:
        if branch_name in worktree_paths_by_branch:
            continue
        try:
            worktree_paths_by_branch[branch_name] = await service.add_worktree(
                working_directory,
                branch_name,
                from_ref=None,
            )
        except GitError as e:
            raise QueuePlanError(
                f"Failed to create or resolve worktree for branch `{branch_name}`: {e}"
            ) from e

    return [
        MaterializedQueuePlanPrompt(
            prompt=item.prompt,
            working_directory_override=(
                worktree_paths_by_branch[item.branch_name] if item.branch_name else None
            ),
        )
        for item in expanded
    ]


def _parse_to_ast(text: str) -> list[_Node]:
    stack: list[_Frame] = [_Frame(kind="root", start_line=0)]

    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = _parse_marker(line.strip(), strict=True)
        current = stack[-1]

        if marker is None:
            current.prompt_lines.append(line)
            continue

        _flush_prompt(current)
        marker_type = marker[0]

        if marker_type == "separator":
            continue

        if marker_type == "branch_start":
            stack.append(_Frame(kind="branch", start_line=line_number, branch_name=marker[1]))
            continue

        if marker_type == "branch_end":
            branch_name = marker[1]
            if current.kind != "branch":
                raise QueuePlanError(
                    f"Line {line_number}: found `***branch-{branch_name}-end***` "
                    "without matching open branch block."
                )
            if current.branch_name != branch_name:
                raise QueuePlanError(
                    f"Line {line_number}: branch end `{branch_name}` does not match open "
                    f"branch `{current.branch_name}` from line {current.start_line}."
                )
            finished = stack.pop()
            stack[-1].nodes.append(
                _BranchNode(branch_name=finished.branch_name or "", children=finished.nodes)
            )
            continue

        if marker_type == "loop_start":
            stack.append(_Frame(kind="loop", start_line=line_number, loop_count=marker[1]))
            continue

        if marker_type == "loop_end":
            loop_count = marker[1]
            if current.kind != "loop":
                raise QueuePlanError(
                    f"Line {line_number}: found `***loop-{loop_count}-end***` "
                    "without matching open loop block."
                )
            if current.loop_count != loop_count:
                raise QueuePlanError(
                    f"Line {line_number}: loop end `{loop_count}` does not match open "
                    f"loop `{current.loop_count}` from line {current.start_line}."
                )
            finished = stack.pop()
            stack[-1].nodes.append(
                _LoopNode(count=finished.loop_count or 1, children=finished.nodes)
            )
            continue

        raise QueuePlanError(f"Line {line_number}: unsupported queue-plan marker.")

    _flush_prompt(stack[-1])
    if len(stack) != 1:
        unclosed = stack[-1]
        if unclosed.kind == "branch":
            marker = f"***branch-{unclosed.branch_name}***"
        else:
            marker = f"***loop-{unclosed.loop_count}***"
        raise QueuePlanError(f"Unclosed block `{marker}` started on line {unclosed.start_line}.")

    return stack[0].nodes


def _expand_nodes(
    nodes: list[_Node],
    active_branch: Optional[str],
    out: list[QueuePlanPrompt],
    max_items: int,
) -> None:
    for node in nodes:
        if isinstance(node, _PromptNode):
            if len(out) >= max_items:
                raise QueuePlanError(
                    f"Structured queue plan expands to more than {max_items} items."
                )
            out.append(QueuePlanPrompt(prompt=node.prompt, branch_name=active_branch))
            continue

        if isinstance(node, _BranchNode):
            _expand_nodes(
                node.children, active_branch=node.branch_name, out=out, max_items=max_items
            )
            continue

        if isinstance(node, _LoopNode):
            for _ in range(node.count):
                _expand_nodes(
                    node.children, active_branch=active_branch, out=out, max_items=max_items
                )
            continue

        raise QueuePlanError("Unsupported queue-plan node type.")


def _flush_prompt(frame: _Frame) -> None:
    if not frame.prompt_lines:
        return

    raw_text = "\n".join(frame.prompt_lines).strip("\n")
    frame.prompt_lines.clear()
    if raw_text.strip():
        frame.nodes.append(_PromptNode(prompt=raw_text))


def _parse_marker(line: str, strict: bool) -> tuple[str, str | int] | tuple[str] | None:
    if line == "***":
        return ("separator",)

    loop_end = _LOOP_END_RE.match(line)
    if loop_end:
        count = int(loop_end.group(1))
        if count < 1:
            raise QueuePlanError(
                f"Invalid loop count `{count}` in marker `{line}`. Loop counts must be >= 1."
            )
        return ("loop_end", count)

    loop_start = _LOOP_START_RE.match(line)
    if loop_start:
        count = int(loop_start.group(1))
        if count < 1:
            raise QueuePlanError(
                f"Invalid loop count `{count}` in marker `{line}`. Loop counts must be >= 1."
            )
        return ("loop_start", count)

    branch_end = _BRANCH_END_RE.match(line)
    if branch_end:
        branch_name = branch_end.group(1).strip()
        if not branch_name:
            raise QueuePlanError("Branch end marker must include a branch name.")
        return ("branch_end", branch_name)

    branch_start = _BRANCH_START_RE.match(line)
    if branch_start:
        branch_name = branch_start.group(1).strip()
        if not branch_name:
            raise QueuePlanError("Branch marker must include a branch name.")
        return ("branch_start", branch_name)

    if strict and _ANY_MARKER_RE.match(line):
        raise QueuePlanError(f"Unknown queue-plan marker: `{line}`")
    return None
