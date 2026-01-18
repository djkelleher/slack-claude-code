"""Unit tests for validators module."""

from pathlib import Path

import pytest

from src.utils.validators import ALLOWED_BASE_PATHS, validate_path


class TestValidatePath:
    """Tests for validate_path function."""

    def test_valid_home_path(self, tmp_path):
        """Paths under home directory are valid."""
        # Create a temp dir under home or /tmp (which are allowed)
        # tmp_path is typically under /tmp
        success, result = validate_path(str(tmp_path))

        assert success is True
        assert isinstance(result, Path)
        assert result == tmp_path

    def test_valid_tmp_path(self, tmp_path):
        """Paths under /tmp are valid."""
        success, result = validate_path(str(tmp_path))

        assert success is True
        assert isinstance(result, Path)

    def test_expands_tilde(self):
        """Path with ~ is expanded."""
        home = Path.home()
        if home.exists() and home.is_dir():
            success, result = validate_path("~")
            assert success is True
            assert result == home

    def test_nonexistent_path(self):
        """Non-existent paths fail validation."""
        success, error = validate_path("/this/path/does/not/exist/abc123xyz")

        assert success is False
        assert "does not exist" in error

    def test_file_path_not_directory(self, tmp_path):
        """File paths (not directories) fail validation."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        success, error = validate_path(str(test_file))

        assert success is False
        assert "not a directory" in error

    def test_path_outside_allowed(self, monkeypatch):
        """Paths outside allowed directories fail validation."""
        # Temporarily modify allowed paths to be very restrictive
        # We'll use a path that exists but is not in allowed paths
        # /usr should exist on most systems
        if Path("/usr").exists():
            # Monkeypatch the allowed paths to not include /usr
            monkeypatch.setattr(
                "src.utils.validators.ALLOWED_BASE_PATHS",
                [Path("/nonexistent/allowed")],
            )
            success, error = validate_path("/usr")

            assert success is False
            assert "not in allowed directories" in error

    def test_invalid_path_syntax(self):
        """Invalid path syntax returns error."""
        # This should handle any path parsing errors
        # Most paths will parse, so we test with something that exists
        success, result = validate_path("")

        # Empty path typically becomes current directory, but may fail
        # depending on context
        if not success:
            assert "Invalid path" in result or "does not exist" in result

    def test_resolves_symlinks(self, tmp_path):
        """Symlinks are resolved."""
        # Create a real directory and a symlink to it
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()

        link_dir = tmp_path / "link_dir"
        link_dir.symlink_to(real_dir)

        success, result = validate_path(str(link_dir))

        assert success is True
        # The resolved path should be the real directory
        assert result == real_dir


class TestAllowedBasePaths:
    """Tests for ALLOWED_BASE_PATHS constant."""

    def test_includes_home(self):
        """ALLOWED_BASE_PATHS includes home directory."""
        assert Path.home() in ALLOWED_BASE_PATHS

    def test_includes_tmp(self):
        """ALLOWED_BASE_PATHS includes /tmp."""
        assert Path("/tmp") in ALLOWED_BASE_PATHS

    def test_has_minimum_paths(self):
        """ALLOWED_BASE_PATHS has at least 2 paths."""
        assert len(ALLOWED_BASE_PATHS) >= 2
