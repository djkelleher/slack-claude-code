import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.config import config, get_backend_for_model


@dataclass
class Session:
    id: Optional[int] = None
    channel_id: str = ""
    thread_ts: Optional[str] = None  # Thread timestamp for thread-based sessions
    working_directory: str = "~"
    claude_session_id: Optional[str] = None  # For Claude --resume flag
    permission_mode: Optional[str] = (
        None  # Per-session permission mode override (Claude)
    )
    model: Optional[str] = (
        None  # Model to use (e.g., "sonnet", "claude-opus-4-6[1m]", "gpt-5.3-codex")
    )
    added_dirs: list[str] = field(
        default_factory=list
    )  # Directories added via /add-dir
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    # Codex-specific fields
    codex_session_id: Optional[str] = None  # For Codex resume
    sandbox_mode: str = (
        config.CODEX_SANDBOX_MODE
    )  # read-only, workspace-write, danger-full-access
    approval_mode: str = config.CODEX_APPROVAL_MODE  # untrusted, on-request, never

    @classmethod
    def from_row(cls, row: tuple) -> "Session":
        return cls(
            id=row[0],
            channel_id=row[1],
            thread_ts=row[2],
            working_directory=row[3],
            claude_session_id=row[4],
            permission_mode=row[5],
            model=row[8],
            added_dirs=json.loads(row[9]) if row[9] else [],
            created_at=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
            last_active=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            codex_session_id=row[10],
            sandbox_mode=row[11] or config.CODEX_SANDBOX_MODE,
            approval_mode=row[12] or config.CODEX_APPROVAL_MODE,
        )

    def is_thread_session(self) -> bool:
        """Check if this is a thread-scoped session."""
        return self.thread_ts is not None

    def session_display_name(self) -> str:
        """Get human-readable session identifier."""
        if self.is_thread_session():
            return f"{self.channel_id} (Thread: {self.thread_ts})"
        return f"{self.channel_id} (Channel)"

    def get_backend(self) -> str:
        """Get the backend type based on the current model.

        Returns
        -------
        str
            "claude" or "codex"
        """
        return get_backend_for_model(self.model)


@dataclass
class CommandHistory:
    id: Optional[int] = None
    session_id: int = 0
    command: str = ""
    output: Optional[str] = None
    detailed_output: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "CommandHistory":
        detailed_output = row[4] if len(row) > 8 else None
        status = row[5] if len(row) > 8 else row[4]
        error_message = row[6] if len(row) > 8 else row[5]
        created_at = row[7] if len(row) > 8 else row[6]
        completed_at = row[8] if len(row) > 8 else row[7]
        return cls(
            id=row[0],
            session_id=row[1],
            command=row[2],
            output=row[3],
            detailed_output=detailed_output,
            status=status,
            error_message=error_message,
            created_at=(
                datetime.fromisoformat(created_at) if created_at else datetime.now()
            ),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
        )


@dataclass
class ParallelJob:
    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    job_type: str = ""  # parallel_analysis, sequential_loop
    status: str = "pending"  # pending, running, completed, failed, cancelled
    config: dict = field(default_factory=dict)  # n_instances, commands, loop_count
    results: list = field(default_factory=list)  # outputs from each terminal
    aggregation_output: Optional[str] = None
    message_ts: Optional[str] = None  # Slack message timestamp for updates
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "ParallelJob":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            job_type=row[3],
            status=row[4],
            config=json.loads(row[5]) if row[5] else {},
            results=json.loads(row[6]) if row[6] else [],
            aggregation_output=row[7],
            message_ts=row[8],
            created_at=datetime.fromisoformat(row[9]) if row[9] else datetime.now(),
            completed_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )


@dataclass
class QueueItem:
    """Item in the FIFO command queue."""

    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    thread_ts: Optional[str] = None
    prompt: str = ""
    working_directory_override: Optional[str] = None
    parallel_group_id: Optional[str] = None
    parallel_limit: Optional[int] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    output: Optional[str] = None
    error_message: Optional[str] = None
    position: int = 0
    message_ts: Optional[str] = None
    automation_meta: Optional[dict[str, object]] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "QueueItem":
        created_at_index = 13
        started_at_index = 14
        completed_at_index = 15
        raw_automation_meta = None
        if len(row) > 16:
            raw_automation_meta = row[13]
            created_at_index = 14
            started_at_index = 15
            completed_at_index = 16

        automation_meta = None
        if raw_automation_meta:
            try:
                parsed = json.loads(raw_automation_meta)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                automation_meta = parsed
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            thread_ts=row[3],
            prompt=row[4],
            working_directory_override=row[5],
            parallel_group_id=row[6],
            parallel_limit=row[7],
            status=row[8],
            output=row[9],
            error_message=row[10],
            position=row[11],
            message_ts=row[12],
            automation_meta=automation_meta,
            created_at=(
                datetime.fromisoformat(row[created_at_index])
                if row[created_at_index]
                else datetime.now()
            ),
            started_at=(
                datetime.fromisoformat(row[started_at_index])
                if row[started_at_index]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row[completed_at_index])
                if row[completed_at_index]
                else None
            ),
        )


@dataclass
class WorkspaceLease:
    """Active or historical workspace lease for one execution."""

    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    thread_ts: Optional[str] = None
    session_scope: str = ""
    execution_id: str = ""
    repo_root: Optional[str] = None
    target_worktree_path: Optional[str] = None
    target_branch: Optional[str] = None
    leased_root: str = ""
    leased_cwd: str = ""
    base_cwd: str = ""
    relative_subdir: Optional[str] = None
    lease_kind: str = "direct"  # direct, worktree
    worktree_name: Optional[str] = None
    worktree_origin: Optional[str] = None
    merge_status: Optional[str] = None
    status: str = (
        "active"  # active, released, abandoned, merged, needs_manual_attention
    )
    created_at: datetime = field(default_factory=datetime.now)
    released_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "WorkspaceLease":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            thread_ts=row[3],
            session_scope=row[4],
            execution_id=row[5],
            repo_root=row[6],
            target_worktree_path=row[7],
            target_branch=row[8],
            leased_root=row[9],
            leased_cwd=row[10],
            base_cwd=row[11],
            relative_subdir=row[12],
            lease_kind=row[13],
            worktree_name=row[14],
            worktree_origin=row[15],
            merge_status=row[16],
            status=row[17],
            created_at=datetime.fromisoformat(row[18]) if row[18] else datetime.now(),
            released_at=datetime.fromisoformat(row[19]) if row[19] else None,
        )


@dataclass
class UploadedFile:
    """File uploaded from Slack and stored locally."""

    id: Optional[int] = None
    session_id: int = 0
    slack_file_id: str = ""
    filename: str = ""
    mimetype: str = ""
    size: int = 0
    local_path: str = ""
    uploaded_at: datetime = field(default_factory=datetime.now)
    last_referenced: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "UploadedFile":
        return cls(
            id=row[0],
            session_id=row[1],
            slack_file_id=row[2],
            filename=row[3],
            mimetype=row[4],
            size=row[5],
            local_path=row[6],
            uploaded_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            last_referenced=datetime.fromisoformat(row[8]) if row[8] else None,
        )


@dataclass
class GitCheckpoint:
    """Git checkpoint for version control."""

    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    name: str = ""
    stash_ref: str = ""
    stash_message: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    is_auto: bool = False

    @classmethod
    def from_row(cls, row: tuple) -> "GitCheckpoint":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            name=row[3],
            stash_ref=row[4],
            stash_message=row[5],
            description=row[6],
            created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            is_auto=bool(row[8]),
        )


@dataclass
class NotificationSettings:
    """Per-channel notification settings."""

    id: Optional[int] = None
    channel_id: str = ""
    notify_on_completion: bool = True  # Default enabled
    notify_on_permission: bool = True  # Default enabled
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_row(cls, row: tuple) -> "NotificationSettings":
        return cls(
            id=row[0],
            channel_id=row[1],
            notify_on_completion=bool(row[2]),
            notify_on_permission=bool(row[3]),
            created_at=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
            updated_at=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
        )

    @classmethod
    def default(cls, channel_id: str) -> "NotificationSettings":
        """Return default settings for a channel (all notifications enabled)."""
        return cls(channel_id=channel_id)


@dataclass
class QueueControl:
    """Per-scope queue execution state."""

    id: Optional[int] = None
    channel_id: str = ""
    thread_ts: Optional[str] = None
    state: str = "running"
    auto_finish_pending: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_row(cls, row: tuple) -> "QueueControl":
        return cls(
            id=row[0],
            channel_id=row[1],
            thread_ts=row[2],
            state=row[3] or "running",
            auto_finish_pending=bool(row[6]) if len(row) > 6 else False,
            created_at=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
            updated_at=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
        )

    @classmethod
    def default(cls, channel_id: str, thread_ts: Optional[str]) -> "QueueControl":
        """Return the default running state for a queue scope."""
        return cls(
            channel_id=channel_id,
            thread_ts=thread_ts,
            state="running",
            auto_finish_pending=False,
        )


@dataclass
class QueueScheduledEvent:
    """Scheduled queue control event for a channel/thread scope."""

    id: Optional[int] = None
    channel_id: str = ""
    thread_ts: Optional[str] = None
    action: str = ""  # start, pause, resume, stop
    execute_at: datetime = field(default_factory=datetime.now)
    status: str = "pending"  # pending, executed, failed, cancelled
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    executed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "QueueScheduledEvent":
        return cls(
            id=row[0],
            channel_id=row[1],
            thread_ts=row[2],
            action=row[3],
            execute_at=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
            status=row[5] or "pending",
            error_message=row[6],
            created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            executed_at=datetime.fromisoformat(row[8]) if row[8] else None,
        )
