"""Unit tests for git models and service."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.git.models import Checkpoint, GitStatus
from src.git.service import GitError, GitService


class TestGitStatus:
    """Tests for GitStatus model."""

    def test_default_values(self):
        """GitStatus has correct defaults."""
        status = GitStatus()

        assert status.branch == "unknown"
        assert status.modified == []
        assert status.staged == []
        assert status.untracked == []
        assert status.ahead == 0
        assert status.behind == 0
        assert status.is_clean is False

    def test_has_changes_with_modified(self):
        """has_changes returns True when modified files exist."""
        status = GitStatus(modified=["file.py"])
        assert status.has_changes() is True

    def test_has_changes_with_staged(self):
        """has_changes returns True when staged files exist."""
        status = GitStatus(staged=["file.py"])
        assert status.has_changes() is True

    def test_has_changes_with_untracked(self):
        """has_changes returns True when untracked files exist."""
        status = GitStatus(untracked=["new_file.py"])
        assert status.has_changes() is True

    def test_has_changes_clean(self):
        """has_changes returns False when no changes."""
        status = GitStatus()
        assert status.has_changes() is False

    def test_summary_clean(self):
        """summary shows clean status."""
        status = GitStatus(branch="main", is_clean=True)
        assert status.summary() == "Branch: main | (clean)"

    def test_summary_with_changes(self):
        """summary shows file counts."""
        status = GitStatus(
            branch="feature",
            staged=["a.py", "b.py"],
            modified=["c.py"],
            untracked=["d.py", "e.py", "f.py"],
        )
        summary = status.summary()
        assert "Branch: feature" in summary
        assert "2 staged" in summary
        assert "1 modified" in summary
        assert "3 untracked" in summary

    def test_summary_with_ahead_behind(self):
        """summary shows ahead/behind counts."""
        status = GitStatus(branch="main", ahead=3, behind=2, is_clean=True)
        summary = status.summary()
        assert "3 ahead" in summary
        assert "2 behind" in summary


class TestCheckpoint:
    """Tests for Checkpoint model."""

    def test_default_values(self):
        """Checkpoint has correct defaults."""
        checkpoint = Checkpoint(name="test", stash_ref="stash@{0}")

        assert checkpoint.name == "test"
        assert checkpoint.stash_ref == "stash@{0}"
        assert checkpoint.message is None
        assert checkpoint.description is None
        assert checkpoint.is_auto is False

    def test_display_name_regular(self):
        """display_name for regular checkpoint."""
        checkpoint = Checkpoint(name="before-refactor", stash_ref="stash@{0}")
        assert checkpoint.display_name() == "before-refactor"

    def test_display_name_auto(self):
        """display_name for auto checkpoint."""
        checkpoint = Checkpoint(name="auto-save", stash_ref="stash@{0}", is_auto=True)
        assert checkpoint.display_name() == "auto-save (auto)"


class TestGitServiceValidation:
    """Tests for GitService validation methods."""

    def test_validate_branch_name_valid(self):
        """Valid branch names pass validation."""
        service = GitService()
        # These should not raise
        service._validate_branch_name("main")
        service._validate_branch_name("feature/add-auth")
        service._validate_branch_name("bugfix-123")
        service._validate_branch_name("release-v1.0.0")

    def test_validate_branch_name_empty(self):
        """Empty branch name raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot be empty"):
            service._validate_branch_name("")

    def test_validate_branch_name_whitespace_only(self):
        """Whitespace-only branch name raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot be empty"):
            service._validate_branch_name("   ")

    def test_validate_branch_name_too_long(self):
        """Branch name over 255 chars raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="too long"):
            service._validate_branch_name("a" * 256)

    def test_validate_branch_name_invalid_chars(self):
        """Branch names with invalid characters raise GitError."""
        service = GitService()
        invalid_names = [
            "feature branch",  # space
            "feature~name",  # tilde
            "feature^name",  # caret
            "feature:name",  # colon
            "feature?name",  # question mark
            "feature*name",  # asterisk
            "feature[name",  # bracket
            "feature\\name",  # backslash
            "feature..name",  # double dot
            "feature@{name",  # @{
            "feature//name",  # double slash
        ]
        for name in invalid_names:
            with pytest.raises(GitError, match="invalid character"):
                service._validate_branch_name(name)

    def test_validate_branch_name_leading_trailing_slash(self):
        """Branch names starting/ending with slash raise GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot start or end with"):
            service._validate_branch_name("/feature")
        with pytest.raises(GitError, match="cannot start or end with"):
            service._validate_branch_name("feature/")

    def test_validate_branch_name_leading_trailing_dot(self):
        """Branch names starting/ending with dot raise GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot start or end with"):
            service._validate_branch_name(".feature")
        with pytest.raises(GitError, match="cannot start or end with"):
            service._validate_branch_name("feature.")

    def test_validate_branch_name_lock_suffix(self):
        """Branch names ending with .lock raise GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot end with"):
            service._validate_branch_name("feature.lock")

    def test_validate_commit_message_valid(self):
        """Valid commit messages pass validation."""
        service = GitService()
        service._validate_commit_message("Fix bug in auth flow")
        service._validate_commit_message("Add new feature\n\nDetailed description here.")

    def test_validate_commit_message_empty(self):
        """Empty commit message raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot be empty"):
            service._validate_commit_message("")

    def test_validate_commit_message_whitespace_only(self):
        """Whitespace-only commit message raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="cannot be empty"):
            service._validate_commit_message("   ")

    def test_validate_commit_message_too_long(self):
        """Commit message over 10000 chars raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="too long"):
            service._validate_commit_message("a" * 10001)

    def test_validate_working_directory_not_exists(self):
        """Non-existent directory raises GitError."""
        service = GitService()
        with pytest.raises(GitError, match="does not exist"):
            service._validate_working_directory("/nonexistent/path/abc123")

    def test_validate_working_directory_is_file(self, tmp_path):
        """File path (not directory) raises GitError."""
        test_file = tmp_path / "testfile.txt"
        test_file.write_text("test")

        service = GitService()
        with pytest.raises(GitError, match="Not a directory"):
            service._validate_working_directory(str(test_file))


class TestGitServiceAsync:
    """Async tests for GitService."""

    @pytest.mark.asyncio
    async def test_validate_git_repo_true(self, tmp_path):
        """validate_git_repo returns True for git repos."""
        # Create a minimal git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        service = GitService()
        with patch.object(service, "_run_git_command") as mock_cmd:
            mock_cmd.return_value = ("", "", 0)
            result = await service.validate_git_repo(str(tmp_path))
            assert result is True

    @pytest.mark.asyncio
    async def test_validate_git_repo_false(self, tmp_path):
        """validate_git_repo returns False for non-repos."""
        service = GitService()
        with patch.object(service, "_run_git_command") as mock_cmd:
            mock_cmd.return_value = ("", "fatal: not a git repository", 128)
            result = await service.validate_git_repo(str(tmp_path))
            assert result is False

    @pytest.mark.asyncio
    async def test_validate_git_repo_exception(self, tmp_path):
        """validate_git_repo returns False on exception."""
        service = GitService()
        with patch.object(service, "_run_git_command") as mock_cmd:
            mock_cmd.side_effect = Exception("Command failed")
            result = await service.validate_git_repo(str(tmp_path))
            assert result is False

    @pytest.mark.asyncio
    async def test_get_diff_not_git_repo(self, tmp_path):
        """get_diff raises GitError for non-repos."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=False):
            with pytest.raises(GitError, match="Not a git repository"):
                await service.get_diff(str(tmp_path))

    @pytest.mark.asyncio
    async def test_get_diff_staged(self, tmp_path):
        """get_diff with staged=True uses --staged flag."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("diff output", "", 0)
                result = await service.get_diff(str(tmp_path), staged=True)
                mock_cmd.assert_called_once_with(str(tmp_path), "diff", "--staged")
                assert result == "diff output"

    @pytest.mark.asyncio
    async def test_get_diff_no_changes(self, tmp_path):
        """get_diff returns '(no changes)' when empty."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "", 0)
                result = await service.get_diff(str(tmp_path))
                assert result == "(no changes)"

    @pytest.mark.asyncio
    async def test_get_diff_truncates_large_output(self, tmp_path):
        """get_diff truncates output exceeding max_size."""
        service = GitService()
        large_diff = "x" * 2000

        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (large_diff, "", 0)
                result = await service.get_diff(str(tmp_path), max_size=1000)
                assert len(result) < len(large_diff)
                assert "truncated" in result

    @pytest.mark.asyncio
    async def test_get_status_parses_output(self, tmp_path):
        """get_status parses git status --short output."""
        service = GitService()
        status_output = """M  modified_file.py
 M unstaged_file.py
A  staged_new.py
?? untracked.py
D  deleted.py"""

        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                # Mock branch command
                mock_cmd.side_effect = [
                    ("feature-branch", "", 0),  # branch --show-current
                    ("2\t1", "", 0),  # rev-list --left-right
                    (status_output, "", 0),  # status --short
                ]
                result = await service.get_status(str(tmp_path))

                assert result.branch == "feature-branch"
                assert result.ahead == 2
                assert result.behind == 1
                assert "modified_file.py" in result.staged
                assert "unstaged_file.py" in result.modified
                assert "untracked.py" in result.untracked

    @pytest.mark.asyncio
    async def test_commit_changes_validates_message(self, tmp_path):
        """commit_changes validates commit message."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with pytest.raises(GitError, match="cannot be empty"):
                await service.commit_changes(str(tmp_path), "")

    @pytest.mark.asyncio
    async def test_create_branch_validates_name(self, tmp_path):
        """create_branch validates branch name."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with pytest.raises(GitError, match="invalid character"):
                await service.create_branch(str(tmp_path), "feature branch")

    @pytest.mark.asyncio
    async def test_get_branches_parses_output(self, tmp_path):
        """get_branches parses branch list output."""
        service = GitService()
        branch_output = """  develop
* main
  feature/auth"""

        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (branch_output, "", 0)
                branches, current = await service.get_branches(str(tmp_path))

                assert current == "main"
                assert "main" in branches
                assert "develop" in branches
                assert "feature/auth" in branches
                assert len(branches) == 3

    @pytest.mark.asyncio
    async def test_run_git_command_timeout(self, tmp_path):
        """_run_git_command handles timeout."""
        service = GitService(timeout=0.001)  # Very short timeout

        # Create the directory so validation passes
        with patch.object(service, "_validate_working_directory"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
                mock_process.terminate = MagicMock()
                mock_process.kill = MagicMock()
                mock_process.wait = AsyncMock()
                mock_process.returncode = None
                mock_exec.return_value = mock_process

                with pytest.raises(GitError, match="timed out"):
                    await service._run_git_command(str(tmp_path), "status")
