"""Unit tests for git worktree models and service methods."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.git.models import Worktree
from src.git.service import GitError, GitService


class TestWorktreeModel:
    """Tests for Worktree dataclass."""

    def test_default_values(self):
        """Worktree has correct defaults."""
        wt = Worktree(path="/tmp/project", branch="main")
        assert wt.path == "/tmp/project"
        assert wt.branch == "main"
        assert wt.commit == ""
        assert wt.is_main is False

    def test_with_all_fields(self):
        """Worktree with all fields set."""
        wt = Worktree(
            path="/tmp/project",
            branch="main",
            commit="abc123",
            is_main=True,
        )
        assert wt.commit == "abc123"
        assert wt.is_main is True


class TestListWorktrees:
    """Tests for GitService.list_worktrees."""

    @pytest.mark.asyncio
    async def test_parses_porcelain_output(self, tmp_path):
        """list_worktrees parses git worktree list --porcelain output."""
        service = GitService()
        porcelain_output = (
            "worktree /home/user/project\n"
            "HEAD abc123def456\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/project-worktrees/feature-x\n"
            "HEAD def456abc789\n"
            "branch refs/heads/feature-x\n"
            "\n"
        )
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (porcelain_output, "", 0)
                result = await service.list_worktrees(str(tmp_path))

                assert len(result) == 2
                assert result[0].path == "/home/user/project"
                assert result[0].branch == "main"
                assert result[0].commit == "abc123def456"
                assert result[0].is_main is True
                assert result[1].path == "/home/user/project-worktrees/feature-x"
                assert result[1].branch == "feature-x"
                assert result[1].is_main is False

    @pytest.mark.asyncio
    async def test_handles_detached_head(self, tmp_path):
        """list_worktrees handles detached HEAD worktrees."""
        service = GitService()
        porcelain_output = (
            "worktree /home/user/project\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/project-worktrees/detached\n"
            "HEAD def456\n"
            "detached\n"
            "\n"
        )
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (porcelain_output, "", 0)
                result = await service.list_worktrees(str(tmp_path))

                assert len(result) == 2
                assert result[1].branch == "(detached HEAD)"

    @pytest.mark.asyncio
    async def test_handles_single_worktree(self, tmp_path):
        """list_worktrees works with just the main worktree."""
        service = GitService()
        porcelain_output = (
            "worktree /home/user/project\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
        )
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (porcelain_output, "", 0)
                result = await service.list_worktrees(str(tmp_path))

                assert len(result) == 1
                assert result[0].is_main is True

    @pytest.mark.asyncio
    async def test_handles_no_trailing_newline(self, tmp_path):
        """list_worktrees handles output without trailing blank line."""
        service = GitService()
        porcelain_output = (
            "worktree /home/user/project\n"
            "HEAD abc123\n"
            "branch refs/heads/main"
        )
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (porcelain_output, "", 0)
                result = await service.list_worktrees(str(tmp_path))

                assert len(result) == 1
                assert result[0].branch == "main"

    @pytest.mark.asyncio
    async def test_not_git_repo(self, tmp_path):
        """list_worktrees raises GitError for non-repos."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=False):
            with pytest.raises(GitError, match="Not a git repository"):
                await service.list_worktrees(str(tmp_path))


class TestGetMainWorktree:
    """Tests for GitService.get_main_worktree."""

    @pytest.mark.asyncio
    async def test_returns_first_worktree_path(self, tmp_path):
        """get_main_worktree returns the first worktree from porcelain output."""
        service = GitService()
        porcelain_output = (
            "worktree /home/user/project\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/project-worktrees/feature\n"
            "HEAD def456\n"
            "branch refs/heads/feature\n"
            "\n"
        )
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = (porcelain_output, "", 0)
                result = await service.get_main_worktree(str(tmp_path))
                assert result == "/home/user/project"

    @pytest.mark.asyncio
    async def test_not_git_repo(self, tmp_path):
        """get_main_worktree raises GitError for non-repos."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=False):
            with pytest.raises(GitError, match="Not a git repository"):
                await service.get_main_worktree(str(tmp_path))


class TestAddWorktree:
    """Tests for GitService.add_worktree."""

    @pytest.mark.asyncio
    async def test_creates_worktree(self, tmp_path):
        """add_worktree creates a worktree in the sibling -worktrees/ directory."""
        service = GitService()
        main_root = str(tmp_path / "project")

        with patch.object(service, "get_main_worktree", return_value=main_root):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "", 0)
                result = await service.add_worktree(str(tmp_path), "feature-x")

                expected_path = str(Path(main_root + "-worktrees") / "feature-x")
                assert result == expected_path

                # Verify git worktree add was called correctly
                mock_cmd.assert_called_once_with(
                    str(tmp_path),
                    "worktree",
                    "add",
                    "-b",
                    "feature-x",
                    expected_path,
                )

    @pytest.mark.asyncio
    async def test_validates_branch_name(self, tmp_path):
        """add_worktree validates branch name."""
        service = GitService()
        with pytest.raises(GitError, match="invalid character"):
            await service.add_worktree(str(tmp_path), "feature branch")

    @pytest.mark.asyncio
    async def test_rejects_existing_path(self, tmp_path):
        """add_worktree raises error if worktree directory already exists."""
        service = GitService()
        main_root = str(tmp_path / "project")
        # Create the worktree path so it already exists
        existing_path = Path(main_root + "-worktrees") / "feature-x"
        existing_path.mkdir(parents=True)

        with patch.object(service, "get_main_worktree", return_value=main_root):
            with pytest.raises(GitError, match="already exists"):
                await service.add_worktree(str(tmp_path), "feature-x")

    @pytest.mark.asyncio
    async def test_git_failure(self, tmp_path):
        """add_worktree raises error on git failure."""
        service = GitService()
        main_root = str(tmp_path / "project")

        with patch.object(service, "get_main_worktree", return_value=main_root):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "fatal: branch already exists", 128)
                with pytest.raises(GitError, match="Failed to create worktree"):
                    await service.add_worktree(str(tmp_path), "feature-x")


class TestRemoveWorktree:
    """Tests for GitService.remove_worktree."""

    @pytest.mark.asyncio
    async def test_removes_worktree(self, tmp_path):
        """remove_worktree calls git worktree remove."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "", 0)
                result = await service.remove_worktree(str(tmp_path), "/tmp/worktree")
                assert result is True
                mock_cmd.assert_called_once_with(
                    str(tmp_path), "worktree", "remove", "/tmp/worktree"
                )

    @pytest.mark.asyncio
    async def test_force_remove(self, tmp_path):
        """remove_worktree with force adds --force flag."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "", 0)
                await service.remove_worktree(str(tmp_path), "/tmp/worktree", force=True)
                mock_cmd.assert_called_once_with(
                    str(tmp_path), "worktree", "remove", "/tmp/worktree", "--force"
                )

    @pytest.mark.asyncio
    async def test_failure_raises_error(self, tmp_path):
        """remove_worktree raises error on failure."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "has changes", 1)
                with pytest.raises(GitError, match="Failed to remove worktree"):
                    await service.remove_worktree(str(tmp_path), "/tmp/worktree")


class TestMergeBranch:
    """Tests for GitService.merge_branch."""

    @pytest.mark.asyncio
    async def test_successful_merge(self, tmp_path):
        """merge_branch returns (True, message) on success."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("Merge made by 'ort' strategy.", "", 0)
                success, message = await service.merge_branch(str(tmp_path), "feature-x")
                assert success is True
                assert "Merge made" in message

    @pytest.mark.asyncio
    async def test_detects_conflicts(self, tmp_path):
        """merge_branch returns (False, details) on merge conflicts."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.side_effect = [
                    ("CONFLICT (content): Merge conflict in file.py", "", 1),
                    ("file.py\nother.py", "", 0),  # diff --name-only
                ]
                success, message = await service.merge_branch(str(tmp_path), "feature-x")
                assert success is False
                assert "file.py" in message
                assert "2 file(s)" in message

    @pytest.mark.asyncio
    async def test_non_conflict_failure(self, tmp_path):
        """merge_branch raises GitError on non-conflict failures."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with patch.object(service, "_run_git_command") as mock_cmd:
                mock_cmd.return_value = ("", "fatal: not something we can merge", 1)
                with pytest.raises(GitError, match="Merge failed"):
                    await service.merge_branch(str(tmp_path), "nonexistent")

    @pytest.mark.asyncio
    async def test_validates_branch_name(self, tmp_path):
        """merge_branch validates the branch name."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=True):
            with pytest.raises(GitError, match="invalid character"):
                await service.merge_branch(str(tmp_path), "feature branch")

    @pytest.mark.asyncio
    async def test_not_git_repo(self, tmp_path):
        """merge_branch raises GitError for non-repos."""
        service = GitService()
        with patch.object(service, "validate_git_repo", return_value=False):
            with pytest.raises(GitError, match="Not a git repository"):
                await service.merge_branch(str(tmp_path), "feature-x")
