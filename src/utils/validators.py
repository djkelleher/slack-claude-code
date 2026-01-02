import json
from pathlib import Path
from typing import Union

# Allowed base paths for security - restrict where commands can operate
ALLOWED_BASE_PATHS = [
    Path.home(),
    Path("/tmp"),
]


def validate_json_commands(json_str: str) -> tuple[bool, Union[list[str], str]]:
    """
    Validate a JSON string as an array of command strings.

    Returns (True, list_of_commands) on success, (False, error_message) on failure.
    """
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"

    if not isinstance(parsed, list):
        return False, "Expected a JSON array of command strings"

    if not parsed:
        return False, "Command array cannot be empty"

    for i, item in enumerate(parsed):
        if not isinstance(item, str):
            return False, f"Item at index {i} is not a string"
        if not item.strip():
            return False, f"Item at index {i} is empty"

    return True, parsed


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


def parse_parallel_args(text: str) -> tuple[bool, Union[tuple[int, str], str]]:
    """
    Parse arguments for /g (gather) command.

    Expected format: <n> <prompt>
    Returns (True, (n, prompt)) on success, (False, error_message) on failure.
    """
    parts = text.strip().split(maxsplit=1)

    if len(parts) < 2:
        return False, "Usage: /g <n> <prompt>"

    try:
        n = int(parts[0])
    except ValueError:
        return False, f"First argument must be a number, got: {parts[0]}"

    if n < 2:
        return False, "Number of terminals must be at least 2"

    if n > 10:
        return False, "Maximum 10 parallel terminals allowed"

    return True, (n, parts[1])


def parse_loop_args(text: str) -> tuple[bool, Union[tuple[int, list[str]], str]]:
    """
    Parse arguments for /l command.

    Expected format: <n> <json_array>
    Returns (True, (n, commands)) on success, (False, error_message) on failure.
    """
    parts = text.strip().split(maxsplit=1)

    if len(parts) < 2:
        return False, "Usage: /l <n> <json_array_of_commands>"

    try:
        n = int(parts[0])
    except ValueError:
        return False, f"First argument must be a number, got: {parts[0]}"

    if n < 1:
        return False, "Loop count must be at least 1"

    if n > 100:
        return False, "Maximum 100 loops allowed"

    valid, result = validate_json_commands(parts[1])
    if not valid:
        return False, result

    return True, (n, result)
