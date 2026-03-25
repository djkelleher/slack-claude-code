"""Workspace lease orchestration for concurrent executions."""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import aiosqlite

from src.config import config
from src.database.models import Session, WorkspaceLease
from src.database.repository import DatabaseRepository

from .service import GitError, GitService


class WorkspaceLeaseError(RuntimeError):
    """Raised when a workspace lease cannot be prepared safely."""


@dataclass(frozen=True)
class PreparedWorkspace:
    """Execution-ready workspace configuration for one prompt run."""

    lease: WorkspaceLease
    session: Session
    persist_session_ids: bool

    @property
    def uses_auto_worktree(self) -> bool:
        """Return True when this execution is isolated in an auto worktree."""
        return self.lease.lease_kind == "worktree"


@dataclass(frozen=True)
class _GitWorkspaceContext:
    """Resolved git context for the current working directory."""

    repo_root: str
    current_worktree_path: str
    current_branch: Optional[str]
    relative_subdir: Optional[str]


class WorkspaceManager:
    """Prepare isolated execution workspaces backed by DB leases."""

    def __init__(
        self,
        *,
        db: DatabaseRepository,
        claude_executor=None,
        codex_executor=None,
        git_service: Optional[GitService] = None,
    ) -> None:
        self.db = db
        self.claude_executor = claude_executor
        self.codex_executor = codex_executor
        self.git_service = git_service or GitService()

    async def prepare_workspace(
        self,
        *,
        session: Session,
        channel_id: str,
        thread_ts: Optional[str],
        session_scope: str,
        execution_id: str,
    ) -> PreparedWorkspace:
        """Prepare an isolated workspace and create its lease."""
        base_cwd = str(Path(session.working_directory).expanduser().resolve())
        git_context = await self._resolve_git_context(base_cwd)
        if git_context is None:
            lease = await self._acquire_non_git_lease(
                session=session,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                base_cwd=base_cwd,
            )
            return PreparedWorkspace(
                lease=lease,
                session=replace(session, working_directory=base_cwd),
                persist_session_ids=True,
            )

        lease = await self._try_acquire_direct_git_lease(
            session=session,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            base_cwd=base_cwd,
            git_context=git_context,
        )
        if lease is not None:
            return PreparedWorkspace(
                lease=lease,
                session=replace(session, working_directory=base_cwd),
                persist_session_ids=True,
            )

        if not git_context.current_branch:
            raise WorkspaceLeaseError(
                "Concurrent execution requires a branch-backed worktree, but the current "
                f"workspace at `{git_context.current_worktree_path}` is detached."
            )

        return await self._prepare_auto_worktree(
            session=session,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            base_cwd=base_cwd,
            git_context=git_context,
        )

    async def release_workspace(
        self,
        execution_id: str,
        *,
        status: str = "released",
        merge_status: Optional[str] = None,
    ) -> bool:
        """Release a workspace lease row."""
        return await self.db.release_workspace_lease(
            execution_id,
            status=status,
            merge_status=merge_status,
        )

    async def cleanup_auto_worktree(
        self,
        lease: WorkspaceLease,
        *,
        delete_branch: bool = True,
    ) -> Optional[str]:
        """Remove a clean auto-created worktree and optionally delete its temp branch."""
        if lease.lease_kind != "worktree":
            return None
        if not lease.target_worktree_path or not lease.worktree_name:
            return None

        await self.git_service.remove_worktree(lease.target_worktree_path, lease.leased_root)
        branch_note = ""
        if delete_branch:
            try:
                await self.git_service.delete_branch(
                    lease.target_worktree_path, lease.worktree_name, force=True
                )
                branch_note = " Temporary branch deleted."
            except GitError as exc:
                branch_note = f" Temporary branch was kept: {exc}"
        return f"Auto worktree `{lease.leased_root}` removed.{branch_note}"

    async def get_unmerged_files(self, working_directory: str) -> list[str]:
        """Return currently unmerged file paths for a worktree."""
        stdout, _stderr, returncode = await self.git_service._run_git_command(
            working_directory, "diff", "--name-only", "--diff-filter=U"
        )
        if returncode != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    async def _acquire_non_git_lease(
        self,
        *,
        session: Session,
        channel_id: str,
        thread_ts: Optional[str],
        session_scope: str,
        execution_id: str,
        base_cwd: str,
    ) -> WorkspaceLease:
        try:
            return await self.db.create_workspace_lease(
                session_id=session.id or 0,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                repo_root=None,
                target_worktree_path=None,
                target_branch=None,
                leased_root=base_cwd,
                leased_cwd=base_cwd,
                base_cwd=base_cwd,
                relative_subdir=None,
                lease_kind="direct",
            )
        except aiosqlite.IntegrityError:
            existing = await self.db.get_active_workspace_lease_by_root(base_cwd)
            if existing and await self._reclaim_if_stale(existing):
                return await self._acquire_non_git_lease(
                    session=session,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    session_scope=session_scope,
                    execution_id=execution_id,
                    base_cwd=base_cwd,
                )
            raise WorkspaceLeaseError(
                f"Another execution is already using `{base_cwd}`. "
                "Non-git directories are serialized by directory."
            )

    async def _try_acquire_direct_git_lease(
        self,
        *,
        session: Session,
        channel_id: str,
        thread_ts: Optional[str],
        session_scope: str,
        execution_id: str,
        base_cwd: str,
        git_context: _GitWorkspaceContext,
    ) -> Optional[WorkspaceLease]:
        try:
            return await self.db.create_workspace_lease(
                session_id=session.id or 0,
                channel_id=channel_id,
                thread_ts=thread_ts,
                session_scope=session_scope,
                execution_id=execution_id,
                repo_root=git_context.repo_root,
                target_worktree_path=git_context.current_worktree_path,
                target_branch=git_context.current_branch,
                leased_root=git_context.current_worktree_path,
                leased_cwd=base_cwd,
                base_cwd=base_cwd,
                relative_subdir=git_context.relative_subdir,
                lease_kind="direct",
            )
        except aiosqlite.IntegrityError:
            existing = await self.db.get_active_workspace_lease_by_root(
                git_context.current_worktree_path
            )
            if existing and await self._reclaim_if_stale(existing):
                return await self._try_acquire_direct_git_lease(
                    session=session,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    session_scope=session_scope,
                    execution_id=execution_id,
                    base_cwd=base_cwd,
                    git_context=git_context,
                )
            return None

    async def _prepare_auto_worktree(
        self,
        *,
        session: Session,
        channel_id: str,
        thread_ts: Optional[str],
        session_scope: str,
        execution_id: str,
        base_cwd: str,
        git_context: _GitWorkspaceContext,
    ) -> PreparedWorkspace:
        worktree_name = self._build_auto_worktree_name(
            channel_id=channel_id,
            thread_ts=thread_ts,
            execution_id=execution_id,
        )
        worktree_root, worktree_origin, bootstrap_session_id = await self._create_auto_worktree(
            session=session,
            execution_id=execution_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            base_cwd=base_cwd,
            current_branch=git_context.current_branch or "",
            worktree_name=worktree_name,
        )
        leased_cwd = self._join_relative_subdirectory(
            worktree_root,
            git_context.relative_subdir,
        )

        lease = await self.db.create_workspace_lease(
            session_id=session.id or 0,
            channel_id=channel_id,
            thread_ts=thread_ts,
            session_scope=session_scope,
            execution_id=execution_id,
            repo_root=git_context.repo_root,
            target_worktree_path=git_context.current_worktree_path,
            target_branch=git_context.current_branch,
            leased_root=worktree_root,
            leased_cwd=leased_cwd,
            base_cwd=base_cwd,
            relative_subdir=git_context.relative_subdir,
            lease_kind="worktree",
            worktree_name=worktree_name,
            worktree_origin=worktree_origin,
            merge_status="pending",
        )

        prepared_session = replace(
            session,
            working_directory=leased_cwd,
            claude_session_id=bootstrap_session_id if session.get_backend() == "claude" else None,
            codex_session_id=None,
        )
        return PreparedWorkspace(lease=lease, session=prepared_session, persist_session_ids=False)

    async def _create_auto_worktree(
        self,
        *,
        session: Session,
        execution_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        base_cwd: str,
        current_branch: str,
        worktree_name: str,
    ) -> tuple[str, str, Optional[str]]:
        if (
            session.get_backend() == "claude"
            and self.claude_executor is not None
            and await self.claude_executor.supports_native_worktree()
        ):
            try:
                before = await self.git_service.list_worktrees(base_cwd)
                before_paths = {worktree.path for worktree in before}
                result = await self.claude_executor.execute(
                    prompt="Acknowledge once the new worktree session is ready.",
                    working_directory=base_cwd,
                    execution_id=f"{execution_id}-worktree-bootstrap",
                    permission_mode=config.DEFAULT_BYPASS_MODE,
                    db_session_id=session.id,
                    model=session.model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    worktree_name=worktree_name,
                )
                if result.success:
                    after = await self.git_service.list_worktrees(base_cwd)
                    for worktree in after:
                        if worktree.path not in before_paths and worktree.branch == worktree_name:
                            return worktree.path, "claude-native", result.session_id
                    for worktree in after:
                        if worktree.path not in before_paths:
                            return worktree.path, "claude-native", result.session_id
            except Exception:
                pass

        worktree_root = await self.git_service.add_worktree(
            base_cwd,
            worktree_name,
            from_ref=current_branch,
        )
        return worktree_root, "git", None

    async def _resolve_git_context(self, base_cwd: str) -> Optional[_GitWorkspaceContext]:
        if not await self.git_service.validate_git_repo(base_cwd):
            return None

        repo_root = str(Path(await self.git_service.get_main_worktree(base_cwd)).resolve())
        worktrees = await self.git_service.list_worktrees(base_cwd)
        resolved_cwd = Path(base_cwd).resolve()
        current_worktree_path = str(resolved_cwd)
        current_branch: Optional[str] = None
        for worktree in worktrees:
            worktree_path = Path(worktree.path).resolve()
            try:
                resolved_cwd.relative_to(worktree_path)
            except ValueError:
                continue
            current_worktree_path = str(worktree_path)
            current_branch = worktree.branch if not worktree.is_detached else None
            break

        relative_subdir = self._relative_subdirectory(base_cwd, current_worktree_path)
        return _GitWorkspaceContext(
            repo_root=repo_root,
            current_worktree_path=current_worktree_path,
            current_branch=current_branch,
            relative_subdir=relative_subdir,
        )

    async def _reclaim_if_stale(self, lease: WorkspaceLease) -> bool:
        if await self._scope_has_live_execution(lease.session_scope):
            return False
        await self.db.mark_workspace_lease_abandoned(lease.execution_id, merge_status="stale")
        return True

    async def _scope_has_live_execution(self, session_scope: str) -> bool:
        executors = [self.claude_executor, self.codex_executor]
        for executor in executors:
            if executor is None:
                continue
            if await executor.has_active_execution(session_scope):
                return True
        return False

    @staticmethod
    def _relative_subdirectory(base_cwd: str, worktree_root: str) -> Optional[str]:
        base_path = Path(base_cwd).resolve()
        root_path = Path(worktree_root).resolve()
        relative = base_path.relative_to(root_path)
        relative_text = str(relative)
        return None if relative_text == "." else relative_text

    @staticmethod
    def _join_relative_subdirectory(worktree_root: str, relative_subdir: Optional[str]) -> str:
        root = Path(worktree_root).resolve()
        if not relative_subdir:
            return str(root)
        return str((root / relative_subdir).resolve())

    @staticmethod
    def _build_auto_worktree_name(
        *,
        channel_id: str,
        thread_ts: Optional[str],
        execution_id: str,
    ) -> str:
        scope = f"{channel_id}-{thread_ts or 'channel'}-{execution_id}"
        normalized = re.sub(r"[^A-Za-z0-9._/-]+", "-", scope).strip("-").lower()
        normalized = normalized.replace("/", "-")
        return f"slack-auto/{normalized[:120]}"
