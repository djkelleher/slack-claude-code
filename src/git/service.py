"""Git operations service for version control integration."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from .models import GitStatus, Checkpoint

logger = logging.getLogger(__name__)


class GitError(Exception):
    """Raised when git operation fails."""

    pass


class GitService:
    """Git operations service for version control integration."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def _run_git_command(
        self, working_directory: str, *args: str
    ) -> tuple[str, str, int]:
        """Run a git command and return (stdout, stderr, returncode)."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            return stdout, stderr, process.returncode

        except asyncio.TimeoutError:
            logger.error(f"Git command timed out: git {' '.join(args)}")
            raise GitError("Git command timed out")
        except Exception as e:
            logger.error(f"Git command failed: {e}")
            raise GitError(f"Git command failed: {e}")

    async def validate_git_repo(self, working_directory: str) -> bool:
        """Check if directory is a git repository."""
        try:
            _, _, returncode = await self._run_git_command(
                working_directory, "rev-parse", "--git-dir"
            )
            return returncode == 0
        except Exception:
            return False

    async def get_status(self, working_directory: str) -> GitStatus:
        """Get git status."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        status = GitStatus()

        # Get branch info
        branch_out, _, branch_code = await self._run_git_command(
            working_directory, "branch", "--show-current"
        )
        if branch_code == 0 and branch_out:
            status.branch = branch_out

        # Get ahead/behind count
        try:
            ahead_behind, _, _ = await self._run_git_command(
                working_directory, "rev-list", "--left-right", "--count", "HEAD...@{u}"
            )
            if ahead_behind:
                parts = ahead_behind.split()
                if len(parts) == 2:
                    status.ahead = int(parts[0])
                    status.behind = int(parts[1])
        except Exception:
            pass  # No upstream branch or other issue

        # Get file status
        status_out, _, status_code = await self._run_git_command(
            working_directory, "status", "--short"
        )

        if status_code == 0:
            for line in status_out.split("\n"):
                if not line:
                    continue

                file_status = line[:2]
                filename = line[3:].strip()

                # Staged files (index column has status)
                if file_status[0] in ("M", "A", "D", "R", "C"):
                    status.staged.append(filename)
                # Modified files (working tree column has status)
                if file_status[1] in ("M", "D"):
                    status.modified.append(filename)
                # Untracked files
                if file_status == "??":
                    status.untracked.append(filename)

            status.is_clean = not status.has_changes()

        return status

    async def get_diff(self, working_directory: str, staged: bool = False) -> str:
        """Get git diff."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["diff"]
        if staged:
            args.append("--staged")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, *args
        )

        if returncode != 0:
            raise GitError(f"Git diff failed: {stderr}")

        return stdout if stdout else "(no changes)"

    async def create_checkpoint(
        self, working_directory: str, name: str, description: Optional[str] = None
    ) -> Checkpoint:
        """Create checkpoint using git stash."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Build stash message
        stash_message = f"checkpoint: {name}"
        if description:
            stash_message += f" - {description}"

        # Create stash
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "stash", "push", "-m", stash_message
        )

        if returncode != 0:
            raise GitError(f"Failed to create checkpoint: {stderr}")

        # Get stash ref (stash@{0} is the most recent)
        stash_ref = "stash@{0}"

        return Checkpoint(
            name=name, stash_ref=stash_ref, message=stash_message, description=description
        )

    async def restore_checkpoint(self, working_directory: str, stash_ref: str) -> bool:
        """Restore from checkpoint."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "stash", "apply", stash_ref
        )

        if returncode != 0:
            raise GitError(f"Failed to restore checkpoint: {stderr}")

        return True

    async def undo_changes(
        self, working_directory: str, files: Optional[list[str]] = None
    ) -> bool:
        """Undo uncommitted changes."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["restore"]
        if files:
            args.extend(files)
        else:
            args.append(".")

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)

        if returncode != 0:
            raise GitError(f"Failed to undo changes: {stderr}")

        return True

    async def commit_changes(
        self,
        working_directory: str,
        message: str,
        files: Optional[list[str]] = None,
    ) -> str:
        """Commit changes."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Stage files if specified
        if files:
            add_args = ["add"] + files
            _, stderr, returncode = await self._run_git_command(
                working_directory, *add_args
            )
            if returncode != 0:
                raise GitError(f"Failed to stage files: {stderr}")

        # Commit
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "commit", "-m", message
        )

        if returncode != 0:
            raise GitError(f"Failed to commit: {stderr}")

        # Get commit hash
        hash_out, _, _ = await self._run_git_command(
            working_directory, "rev-parse", "--short", "HEAD"
        )

        return hash_out if hash_out else "unknown"

    async def create_branch(
        self, working_directory: str, branch_name: str, switch: bool = True
    ) -> bool:
        """Create branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        if switch:
            args = ["checkout", "-b", branch_name]
        else:
            args = ["branch", branch_name]

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)

        if returncode != 0:
            raise GitError(f"Failed to create branch: {stderr}")

        return True

    async def switch_branch(self, working_directory: str, branch_name: str) -> bool:
        """Switch branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "checkout", branch_name
        )

        if returncode != 0:
            raise GitError(f"Failed to switch branch: {stderr}")

        return True

    async def get_branches(self, working_directory: str) -> tuple[list[str], str]:
        """Get list of branches and current branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "branch"
        )

        if returncode != 0:
            raise GitError(f"Failed to get branches: {stderr}")

        branches = []
        current_branch = ""

        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue

            if line.startswith("*"):
                current_branch = line[2:].strip()
                branches.append(current_branch)
            else:
                branches.append(line)

        return branches, current_branch
