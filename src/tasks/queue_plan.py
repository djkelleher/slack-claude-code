"""Structured queue-plan parser for prompt/worktree/loop DSL."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.git.service import GitError, GitService

MAX_EXPANDED_QUEUE_PLAN_ITEMS: int | None = None

_ANY_MARKER_RE = re.compile(r"^\*\*\*.+$")
_DIRECTIVE_RE = re.compile(r"^\(\((.+)\)\)$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_FOR_LOOP_MARKER_RE = re.compile(
    r"^FOR\s+([a-zA-Z][a-zA-Z0-9_]*)\s+IN\s+\(\((.*?)\)\)(?:\s+(.*))?$",
    re.IGNORECASE,
)
_FOR_LOOP_PREFIX_RE = re.compile(
    r"^FOR\s+[a-zA-Z][a-zA-Z0-9_]*\s+IN\s+\(\(",
    re.IGNORECASE,
)
_DOUBLE_PAREN_VARIABLE_RE = re.compile(r"\(\(\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\)\)")
_QUADRUPLE_PAREN_VARIABLE_RE = re.compile(r"\(\(\(\(\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\)\)\)\)")


class QueuePlanError(ValueError):
    """Raised when structured queue-plan parsing or materialization fails."""


@dataclass(frozen=True)
class QueuePlanPrompt:
    """Expanded queue prompt with optional branch context."""

    prompt: str
    branch_name: Optional[str] = None
    parallel_group_id: Optional[str] = None
    parallel_limit: Optional[int] = None


@dataclass(frozen=True)
class MaterializedQueuePlanPrompt:
    """Queue prompt ready for storage, with optional worktree override."""

    prompt: str
    working_directory_override: Optional[str] = None
    parallel_group_id: Optional[str] = None
    parallel_limit: Optional[int] = None


@dataclass(frozen=True)
class QueuePlanSubmissionOptions:
    """Submission-time queue behavior for a structured queue plan."""

    replace_pending: bool = False
    directive_explicit: bool = False
    scheduled_controls: list["QueueScheduledControl"] = field(default_factory=list)
    insertion_mode: str = "append"
    insert_at: Optional[int] = None


@dataclass(frozen=True)
class QueueScheduledControl:
    """A scheduled queue control action parsed from the queue DSL."""

    action: str
    execute_at: datetime


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


@dataclass
class _ParallelNode:
    limit: Optional[int]
    children: list["_Node"]


@dataclass
class _ForNode:
    variable_name: str
    values: list[str]
    children: list["_Node"]


_Node = _PromptNode | _BranchNode | _LoopNode | _ParallelNode | _ForNode
_Marker = tuple[str] | tuple[str, str | int] | tuple[str, str, list[str]]


@dataclass
class _Frame:
    kind: str
    start_line: int
    branch_name: Optional[str] = None
    loop_count: Optional[int] = None
    parallel_limit: Optional[int] = None
    substitution_variable: Optional[str] = None
    substitution_values: Optional[list[str]] = None
    nodes: list[_Node] = field(default_factory=list)
    prompt_lines: list[str] = field(default_factory=list)


def contains_queue_plan_markers(text: str) -> bool:
    """Return True when text includes at least one line-level queue-plan marker."""
    for line in text.splitlines():
        stripped = line.strip()
        try:
            substitution_loop = _parse_for_loop_marker(stripped)
        except QueuePlanError:
            return True
        if substitution_loop is not None:
            return True

        try:
            marker = _parse_marker(stripped)
        except QueuePlanError:
            # Invalid markers should still route through structured-plan handling
            # so users get a clear validation error from the parser.
            return True
        if marker is not None:
            return True
        if stripped.startswith("***"):
            return True
        if stripped.startswith("((") and stripped.endswith("))"):
            return True
    return False


def parse_queue_plan_text(
    text: str, max_expanded_items: int = MAX_EXPANDED_QUEUE_PLAN_ITEMS
) -> list[QueuePlanPrompt]:
    """Parse queue-plan DSL text into expanded prompt entries."""
    if max_expanded_items is not None and max_expanded_items < 1:
        raise QueuePlanError("max_expanded_items must be at least 1")

    root = _parse_to_ast(_expand_combined_block_directive_lines(text))
    expanded: list[QueuePlanPrompt] = []
    _expand_nodes(
        root,
        active_branch=None,
        active_parallel_group_id=None,
        active_parallel_limit=None,
        substitutions={},
        out=expanded,
        max_items=max_expanded_items,
        group_counter=[0],
    )

    if not expanded:
        raise QueuePlanError("No prompts found in structured queue plan.")
    return expanded


def parse_queue_plan_submission(
    text: str, now_utc: Optional[datetime] = None
) -> tuple[QueuePlanSubmissionOptions, str]:
    """Extract top-level queue submission directives from queue-plan text.

    Supported directives must appear before the first non-empty, non-directive line:
    - ``((append))``: append to the current pending queue
    - ``((prepend))``: insert at the front of the pending queue
    - ``((insertN))``: insert at one-based pending queue index ``N``
    - ``((at <time>))``: schedule an implicit resume/start at the given time
    """
    current_now_utc = now_utc or datetime.now(timezone.utc)
    if current_now_utc.tzinfo is None or current_now_utc.tzinfo.utcoffset(current_now_utc) is None:
        raise QueuePlanError("now_utc must be timezone-aware")
    current_now_utc = current_now_utc.astimezone(timezone.utc)
    replace_pending = False
    seen_directive: str | None = None
    scheduled_controls: list[QueueScheduledControl] = []
    insertion_mode = "append"
    insert_at: Optional[int] = None
    body_start_index = 0
    lines = text.splitlines()

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            body_start_index = index + 1
            continue

        parsed_line = _extract_double_paren_directive_parts(stripped)
        if parsed_line is not None:
            directive_parts, trailing_text = parsed_line
            remaining_parts: list[str] = []
            saw_submission_directive = False
            for part in directive_parts:
                directive = _parse_submission_directive_part(part)
                if directive is None:
                    remaining_parts.append(part)
                    continue

                saw_submission_directive = True
                directive_name, directive_value = directive
                if directive_name in {"append", "prepend", "insert"}:
                    if seen_directive is not None and seen_directive != directive_name:
                        raise QueuePlanError(
                            "Queue submission directives conflict. Use only one of "
                            "`((append))`, `((prepend))`, or `((insertN))`."
                        )
                    seen_directive = directive_name
                if directive_name == "append":
                    replace_pending = False
                    insertion_mode = "append"
                    insert_at = None
                elif directive_name == "prepend":
                    replace_pending = False
                    insertion_mode = "prepend"
                    insert_at = 1
                elif directive_name == "insert":
                    replace_pending = False
                    insertion_mode = "insert"
                    insert_at = int(directive_value)
                elif directive_name == "at":
                    time_text, action = directive_value
                    scheduled_controls.append(
                        _parse_queue_timer_directive(
                            time_text=time_text,
                            action=action,
                            now_utc=current_now_utc,
                        )
                    )

            if saw_submission_directive:
                rebuilt_line = _rebuild_double_paren_directive_line(remaining_parts, trailing_text)
                if rebuilt_line:
                    remaining_text = "\n".join([rebuilt_line] + lines[index + 1 :])
                    return (
                        QueuePlanSubmissionOptions(
                            replace_pending=replace_pending,
                            directive_explicit=seen_directive is not None,
                            scheduled_controls=scheduled_controls,
                            insertion_mode=insertion_mode,
                            insert_at=insert_at,
                        ),
                        remaining_text,
                    )
                body_start_index = index + 1
                continue

        break

    remaining_text = "\n".join(lines[body_start_index:])
    return (
        QueuePlanSubmissionOptions(
            replace_pending=replace_pending,
            directive_explicit=seen_directive is not None,
            scheduled_controls=scheduled_controls,
            insertion_mode=insertion_mode,
            insert_at=insert_at,
        ),
        remaining_text,
    )


def _parse_queue_timer_directive(
    time_text: str, action: str, now_utc: datetime
) -> QueueScheduledControl:
    """Parse and validate one queue timer directive."""
    execute_at_utc = _parse_queue_timer_time(time_text, now_utc)
    if execute_at_utc <= now_utc:
        raise QueuePlanError(
            f"Scheduled queue control time `{time_text}` is in the past. " "Use a future timestamp."
        )
    return QueueScheduledControl(action=action, execute_at=execute_at_utc)


def _parse_queue_timer_time(time_text: str, now_utc: datetime) -> datetime:
    """Parse timer text as ISO8601 with timezone or HH:MM server-local time."""
    raw = time_text.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
            raise QueuePlanError(
                f"Invalid queue timer `{time_text}`. ISO datetimes must include a timezone "
                "offset (example: `2026-03-13T18:30:00-04:00`)."
            )
        return parsed.astimezone(timezone.utc)

    hhmm_match = _HHMM_RE.match(raw)
    if hhmm_match:
        hours = int(hhmm_match.group(1))
        minutes = int(hhmm_match.group(2))
        local_now = now_utc.astimezone()
        local_dt = local_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        return local_dt.astimezone(timezone.utc)

    raise QueuePlanError(
        f"Invalid queue timer `{time_text}`. Use ISO datetime with timezone "
        "(for example: `2026-03-13T18:30:00-04:00`) or `HH:MM`."
    )


def _extract_double_paren_directive_parts(line: str) -> Optional[tuple[list[str], Optional[str]]]:
    """Split a ``((...))`` line into directive parts and optional trailing prompt text."""
    if not line.startswith("(("):
        return None

    closing_index = line.find("))")
    if closing_index == -1:
        return None

    body = line[2:closing_index].strip()
    if not body:
        return None

    parts = [part.strip() for part in body.split(",")]
    if any(not part for part in parts):
        raise QueuePlanError(f"Invalid queue-plan marker: `{line}`")

    trailing_text = line[closing_index + 2 :].strip() or None
    return parts, trailing_text


def _rebuild_double_paren_directive_line(parts: list[str], trailing_text: Optional[str]) -> str:
    """Rebuild a directive line from remaining parts plus any inline prompt text."""
    if parts:
        rebuilt = f"(({', '.join(parts)}))"
        if trailing_text:
            rebuilt = f"{rebuilt} {trailing_text}"
        return rebuilt
    return trailing_text or ""


def _expand_combined_block_directive_lines(text: str) -> str:
    """Expand combined block directives into equivalent nested single-marker lines."""
    expanded_lines: list[str] = []
    for line in text.splitlines():
        expanded_lines.extend(_expand_combined_block_directive_line(line))
    return "\n".join(expanded_lines)


def _expand_combined_block_directive_line(line: str) -> list[str]:
    """Expand one combined block directive line, preserving inline prompt text."""
    parsed = _extract_double_paren_directive_parts(line.strip())
    if parsed is None:
        return [line]

    parts, trailing_text = parsed
    if len(parts) <= 1:
        return [line]

    if any(_parse_block_marker_part(part) is None for part in parts):
        return [line]

    expanded = [f"(({part}))" for part in parts]
    if trailing_text:
        expanded[-1] = f"{expanded[-1]} {trailing_text}"
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
        return [
            MaterializedQueuePlanPrompt(
                prompt=item.prompt,
                parallel_group_id=item.parallel_group_id,
                parallel_limit=item.parallel_limit,
            )
            for item in expanded
        ]

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

    current_worktree_path = _find_containing_worktree_path(working_directory, worktrees)
    if current_worktree_path is None:
        current_worktree_path = str(Path(working_directory).resolve())
    current_subdirectory = _relative_subdirectory(working_directory, current_worktree_path)

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
                _join_worktree_subdirectory(
                    worktree_paths_by_branch[item.branch_name], current_subdirectory
                )
                if item.branch_name
                else None
            ),
            parallel_group_id=item.parallel_group_id,
            parallel_limit=item.parallel_limit,
        )
        for item in expanded
    ]


def _parse_to_ast(text: str) -> list[_Node]:
    stack: list[_Frame] = [_Frame(kind="root", start_line=0)]

    for line_number, line in enumerate(text.splitlines(), start=1):
        marker, inline_prompt = _parse_marker_with_inline_prompt(line.strip(), strict=True)
        current = stack[-1]

        if marker is None:
            current.prompt_lines.append(line)
            continue

        _flush_prompt(current)
        marker_type = marker[0]

        if marker_type == "separator":
            if inline_prompt:
                current.prompt_lines.append(inline_prompt)
            continue

        if marker_type == "branch_start":
            branch_name = marker[1]
            stack.append(_Frame(kind="branch", start_line=line_number, branch_name=branch_name))
            if inline_prompt:
                stack[-1].prompt_lines.append(inline_prompt)
            continue

        if marker_type == "for_start":
            stack.append(
                _Frame(
                    kind="for",
                    start_line=line_number,
                    substitution_variable=marker[1],
                    substitution_values=list(marker[2]),
                )
            )
            if inline_prompt:
                stack[-1].prompt_lines.append(inline_prompt)
            continue

        if marker_type == "block_end":
            if inline_prompt:
                raise QueuePlanError(
                    f"Line {line_number}: inline prompt is not supported on end markers."
                )
            if current.kind == "root":
                detail = _unexpected_block_close_detail(current)
                raise QueuePlanError(
                    f"Line {line_number}: found end marker without a matching open block. {detail}"
                )
            _close_frame(stack)
            continue

        if marker_type == "loop_start":
            stack.append(_Frame(kind="loop", start_line=line_number, loop_count=marker[1]))
            if inline_prompt:
                stack[-1].prompt_lines.append(inline_prompt)
            continue

        if marker_type == "parallel_start":
            if current.kind == "parallel":
                raise QueuePlanError(
                    f"Line {line_number}: nested parallel blocks are not supported."
                )
            stack.append(_Frame(kind="parallel", start_line=line_number, parallel_limit=marker[1]))
            if inline_prompt:
                stack[-1].prompt_lines.append(inline_prompt)
            continue

        raise QueuePlanError(f"Line {line_number}: unsupported queue-plan marker.")

    _flush_prompt(stack[-1])
    # End markers are optional. Any still-open blocks are treated as running to EOF.
    while len(stack) > 1:
        _close_frame(stack)

    return stack[0].nodes


def _close_frame(stack: list[_Frame]) -> None:
    """Close the current non-root frame and append it to its parent."""
    finished = stack.pop()
    if finished.kind == "branch":
        stack[-1].nodes.append(
            _BranchNode(branch_name=finished.branch_name or "", children=finished.nodes)
        )
        return
    if finished.kind == "loop":
        stack[-1].nodes.append(_LoopNode(count=finished.loop_count or 1, children=finished.nodes))
        return
    if finished.kind == "parallel":
        stack[-1].nodes.append(
            _ParallelNode(limit=finished.parallel_limit, children=finished.nodes)
        )
        return
    if finished.kind == "for":
        stack[-1].nodes.append(
            _ForNode(
                variable_name=finished.substitution_variable or "",
                values=list(finished.substitution_values or []),
                children=finished.nodes,
            )
        )
        return
    raise QueuePlanError("Unsupported queue-plan frame type.")


def _unexpected_block_close_detail(current: _Frame) -> str:
    """Describe what block is currently open and how to close it."""
    if current.kind == "root":
        return "No block is currently open."
    if current.kind == "branch":
        return (
            f"You are currently inside branch `{current.branch_name or ''}` opened on line "
            f"{current.start_line}. Close it first with `((end))`."
        )
    if current.kind == "loop":
        return (
            f"You are currently inside loop `{current.loop_count or 1}` opened on line "
            f"{current.start_line}. Close it first with `((end))`."
        )
    if current.kind == "parallel":
        return (
            f"You are currently inside parallel block opened on line {current.start_line}. "
            "Close it first with `((end))`."
        )
    if current.kind == "for":
        return (
            "You are currently inside substitution loop "
            f"`{current.substitution_variable or ''}` opened on line {current.start_line}. "
            "Close it first with `((end))`."
        )
    return "A different block is currently open."


def _expand_nodes(
    nodes: list[_Node],
    active_branch: Optional[str],
    active_parallel_group_id: Optional[str],
    active_parallel_limit: Optional[int],
    substitutions: dict[str, str],
    out: list[QueuePlanPrompt],
    max_items: int,
    group_counter: list[int],
) -> None:
    for node in nodes:
        if isinstance(node, _PromptNode):
            if max_items is not None and len(out) >= max_items:
                raise QueuePlanError(
                    f"Structured queue plan expands to more than {max_items} items."
                )
            out.append(
                QueuePlanPrompt(
                    prompt=_substitute_loop_variables(node.prompt, substitutions),
                    branch_name=active_branch,
                    parallel_group_id=active_parallel_group_id,
                    parallel_limit=active_parallel_limit,
                )
            )
            continue

        if isinstance(node, _BranchNode):
            _expand_nodes(
                node.children,
                active_branch=node.branch_name,
                active_parallel_group_id=active_parallel_group_id,
                active_parallel_limit=active_parallel_limit,
                substitutions=substitutions,
                out=out,
                max_items=max_items,
                group_counter=group_counter,
            )
            continue

        if isinstance(node, _LoopNode):
            for _ in range(node.count):
                _expand_nodes(
                    node.children,
                    active_branch=active_branch,
                    active_parallel_group_id=active_parallel_group_id,
                    active_parallel_limit=active_parallel_limit,
                    substitutions=substitutions,
                    out=out,
                    max_items=max_items,
                    group_counter=group_counter,
                )
            continue

        if isinstance(node, _ParallelNode):
            group_counter[0] += 1
            _expand_nodes(
                node.children,
                active_branch=active_branch,
                active_parallel_group_id=f"parallel-{group_counter[0]}",
                active_parallel_limit=node.limit,
                substitutions=substitutions,
                out=out,
                max_items=max_items,
                group_counter=group_counter,
            )
            continue

        if isinstance(node, _ForNode):
            for raw_value in node.values:
                next_substitutions = dict(substitutions)
                next_substitutions[node.variable_name] = _substitute_loop_variables(
                    raw_value, substitutions
                )
                _expand_nodes(
                    node.children,
                    active_branch=active_branch,
                    active_parallel_group_id=active_parallel_group_id,
                    active_parallel_limit=active_parallel_limit,
                    substitutions=next_substitutions,
                    out=out,
                    max_items=max_items,
                    group_counter=group_counter,
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


def _parse_marker_with_inline_prompt(
    line: str, strict: bool
) -> tuple[Optional[_Marker], Optional[str]]:
    """Parse a marker, allowing same-line prompt payload after marker tokens."""
    substitution_loop = _parse_for_loop_marker(line)
    if substitution_loop is not None:
        variable_name, values, inline_prompt = substitution_loop
        return ("for_start", variable_name, values), inline_prompt

    parsed = _extract_double_paren_directive_parts(line)
    if parsed is not None:
        parts, trailing_text = parsed
        if trailing_text:
            marker_token = f"(({', '.join(parts)}))"
            inline_marker = _parse_marker(marker_token)
            if inline_marker is not None:
                return inline_marker, trailing_text

    marker = _parse_marker(line)
    if marker is not None:
        return marker, None

    if line.startswith("(("):
        closing_index = line.find("))")
        if closing_index != -1:
            marker_token = line[: closing_index + 2]
            trailing_text = line[closing_index + 2 :].strip()
            inline_marker = _parse_marker(marker_token)
            if inline_marker is not None:
                return inline_marker, trailing_text or None

    parts = line.split(maxsplit=1)
    if len(parts) == 2:
        marker_token, trailing_text = parts
        inline_marker = _parse_marker(marker_token)
        if inline_marker is not None:
            return inline_marker, trailing_text

    if strict and _looks_like_double_paren_queue_marker(line):
        raise QueuePlanError(f"Unknown queue-plan marker: `{line}`")
    if strict and _ANY_MARKER_RE.match(line):
        raise QueuePlanError(f"Unknown queue-plan marker: `{line}`")
    return None, None


def _parse_marker(line: str) -> Optional[_Marker]:
    if line == "***":
        return ("separator",)

    substitution_loop = _parse_for_loop_marker(line)
    if substitution_loop is not None:
        variable_name, values, _inline_prompt = substitution_loop
        return ("for_start", variable_name, values)

    double_paren_marker = _parse_double_paren_block_marker(line)
    if double_paren_marker is not None:
        return double_paren_marker

    return None


def _parse_double_paren_submission_directive(
    line: str,
) -> tuple[str, str | int | tuple[str, str]] | None:
    """Parse queue submission directives using ``((...))`` syntax."""
    parsed = _extract_double_paren_directive_parts(line)
    if parsed is None:
        return None
    parts, _trailing_text = parsed
    if len(parts) != 1:
        return None
    return _parse_submission_directive_part(parts[0])


def _parse_submission_directive_part(
    body: str,
) -> tuple[str, str | int | tuple[str, str]] | None:
    """Parse one queue submission directive from a directive body fragment."""
    lowered = body.lower()
    if lowered in {"append", "prepend"}:
        return lowered, lowered
    if lowered.startswith("insert"):
        index_text = lowered[len("insert") :].strip()
        if not index_text.isdigit() or int(index_text) < 1:
            raise QueuePlanError("Insert directives must be like `((insert1))`.")
        return "insert", int(index_text)
    if lowered in {"clear", "replace"}:
        raise QueuePlanError("Queue clearing is handled by `/qc clear`, not queue DSL.")
    if lowered.startswith("at "):
        schedule_body = body[3:].strip()
        if not schedule_body:
            raise QueuePlanError(
                "Timer directives must be like `((at 18:30))` or `((at 18:30 pause))`."
            )
        parts = schedule_body.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].lower() in {"start", "pause", "resume", "stop"}:
            return "at", (parts[0].strip(), parts[1].lower())
        return "at", (schedule_body, "resume")
    return None


def _parse_double_paren_block_marker(
    line: str,
) -> tuple[str, str | int] | tuple[str] | None:
    """Parse block markers using ``((...))`` syntax."""
    parsed = _extract_double_paren_directive_parts(line)
    if parsed is None:
        return None
    parts, _trailing_text = parsed
    if len(parts) != 1:
        return None
    return _parse_block_marker_part(parts[0], line)


def _parse_block_marker_part(
    body: str, original_token: Optional[str] = None
) -> tuple[str, str | int] | tuple[str] | None:
    """Parse one block marker from a directive body fragment."""
    lowered = body.lower()
    line = original_token or f"(({body}))"
    if lowered == "parallel":
        return ("parallel_start", None)
    if lowered == "end":
        return ("block_end",)
    if lowered.startswith("parallel"):
        limit_text = lowered[len("parallel") :].strip()
        if limit_text.isdigit():
            return _parse_parallel_marker_value(limit_text, line)
    if lowered.startswith("loop"):
        count_text = lowered[len("loop") :].strip()
        if count_text.isdigit():
            return _parse_loop_marker_value(count_text, line, marker_type="loop_start")
    if lowered.startswith("branch "):
        return _parse_branch_marker_value(body[len("branch ") :], marker_type="branch_start")
    return None


def _looks_like_double_paren_queue_marker(line: str) -> bool:
    """Return True when a ``((...))`` line resembles queue-plan control syntax."""
    parsed = _extract_double_paren_directive_parts(line)
    if parsed is None:
        return False
    parts, _trailing_text = parsed
    return any(_looks_like_queue_directive_part(part) for part in parts)


def _looks_like_queue_directive_part(body: str) -> bool:
    """Return True when a directive fragment resembles queue-plan control syntax."""
    lowered = body.strip().lower()
    return (
        lowered.startswith("end")
        or lowered.startswith("append")
        or lowered.startswith("prepend")
        or lowered.startswith("insert")
        or lowered.startswith("at ")
        or lowered.startswith("clear")
        or lowered.startswith("replace")
        or lowered.startswith("branch ")
        or lowered.startswith("loop")
        or lowered.startswith("parallel")
    )


def _parse_for_loop_marker(
    line: str,
) -> tuple[str, list[str], Optional[str]] | None:
    """Parse a substitution-loop marker using ``FOR name IN ((a, b))`` syntax."""
    match = _FOR_LOOP_MARKER_RE.match(line)
    if not match:
        if _FOR_LOOP_PREFIX_RE.match(line):
            raise QueuePlanError(f"Invalid substitution loop marker: `{line}`")
        return None

    variable_name = match.group(1)
    values_body = match.group(2).strip()
    inline_prompt = match.group(3).strip() or None if match.group(3) else None
    if not values_body:
        raise QueuePlanError(
            f"Invalid substitution loop marker: `{line}`. Include at least one value."
        )

    values = [part.strip() for part in values_body.split(",")]
    if any(not part for part in values):
        raise QueuePlanError(
            f"Invalid substitution loop marker: `{line}`. Values must be comma-separated."
        )
    return variable_name, values, inline_prompt


def _substitute_loop_variables(text: str, substitutions: dict[str, str]) -> str:
    """Apply scoped loop-variable substitutions to prompt text."""
    if not substitutions:
        return text

    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        return substitutions.get(variable_name, match.group(0))

    substituted = _QUADRUPLE_PAREN_VARIABLE_RE.sub(replace, text)
    return _DOUBLE_PAREN_VARIABLE_RE.sub(replace, substituted)


def _parse_loop_marker_value(count_text: str, line: str, marker_type: str) -> tuple[str, int]:
    """Parse and validate loop marker payload."""
    count = int(count_text)
    if count < 1:
        raise QueuePlanError(
            f"Invalid loop count `{count}` in marker `{line}`. Loop counts must be >= 1."
        )
    return marker_type, count


def _parse_branch_marker_value(branch_text: str, marker_type: str) -> tuple[str, str]:
    """Parse and validate branch marker payload."""
    branch_name = branch_text.strip()
    if not branch_name:
        raise QueuePlanError("Branch marker must include a branch name.")
    return marker_type, branch_name


def _parse_parallel_marker_value(limit_text: Optional[str], line: str) -> tuple[str, Optional[int]]:
    """Parse and validate parallel marker payload."""
    if limit_text is None:
        return "parallel_start", None

    limit = int(limit_text)
    if limit < 1:
        raise QueuePlanError(
            f"Invalid parallel width `{limit}` in marker `{line}`. Width must be >= 1."
        )
    return "parallel_start", limit


def _find_containing_worktree_path(working_directory: str, worktrees: list) -> Optional[str]:
    """Return the worktree root containing the current working directory."""
    cwd_path = Path(working_directory).resolve()
    containing_paths = []
    for worktree in worktrees:
        worktree_path = Path(worktree.path).resolve()
        try:
            cwd_path.relative_to(worktree_path)
        except ValueError:
            continue
        containing_paths.append(worktree_path)

    if not containing_paths:
        return None
    return str(max(containing_paths, key=lambda path: len(path.parts)))


def _relative_subdirectory(working_directory: str, worktree_path: str) -> Path:
    """Return cwd relative to its containing worktree root."""
    cwd_path = Path(working_directory).resolve()
    worktree_root = Path(worktree_path).resolve()
    return cwd_path.relative_to(worktree_root)


def _join_worktree_subdirectory(worktree_path: str, subdirectory: Path) -> str:
    """Append a relative session subdirectory to a target worktree root."""
    target_root = Path(worktree_path).resolve()
    if subdirectory == Path("."):
        return str(target_root)
    return str(target_root / subdirectory)
