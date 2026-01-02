from pathlib import Path
from typing import Union

# Allowed base paths for security - restrict where commands can operate
ALLOWED_BASE_PATHS = [
    Path.home(),
    Path("/tmp"),
]


def validate_path(path_str: str) -> tuple[bool, Union[Path, str]]:
    """
    Validate a path string.

    Ensures the path exists, is a directory, and is within allowed base paths
    for security (prevents path traversal attacks).

    Returns (True, Path) on success, (False, error_message) on failure.
    """
    try:
        path = Path(path_str).expanduser().resolve()
    except Exception as e:
        return False, f"Invalid path: {e}"

    if not path.exists():
        return False, f"Path does not exist: {path}"

    if not path.is_dir():
        return False, f"Path is not a directory: {path}"

    # Security: ensure path is under allowed directories
    is_allowed = any(path == base or base in path.parents for base in ALLOWED_BASE_PATHS)
    if not is_allowed:
        return False, "Path not in allowed directories (must be under home or /tmp)"

    return True, path
