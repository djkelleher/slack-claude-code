"""Directory listing formatting."""


def directory_listing(
    path: str, entries: list[tuple[str, bool]], is_cwd: bool = False
) -> list[dict]:
    """Format directory listing for /ls command.

    Parameters
    ----------
    path : str
        The directory path being listed.
    entries : list[tuple[str, bool]]
        List of (name, is_directory) tuples.
    is_cwd : bool
        If True, indicates this is the current working directory.
    """
    if not entries:
        output = "_Directory is empty_"
    else:
        lines = []
        for name, is_dir in entries:
            if is_dir:
                lines.append(f":file_folder: {name}/")
            else:
                lines.append(f":page_facing_up: {name}")

        if len(lines) > 50:
            output = "\n".join(lines[:50]) + f"\n\n_... and {len(lines) - 50} more_"
        else:
            output = "\n".join(lines)

    if is_cwd:
        header = f":open_file_folder: *Current directory:* `{path}`"
    else:
        header = f":open_file_folder: *{path}*"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": header,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output},
        },
    ]


def cwd_updated(new_cwd: str) -> list[dict]:
    """Format CWD update confirmation."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":file_folder: Working directory updated to:\n`{new_cwd}`",
            },
        }
    ]
