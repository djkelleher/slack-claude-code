"""Unit tests for directory formatter helpers."""

from src.utils.formatters.directory import cwd_updated, directory_listing


def test_directory_listing_for_empty_current_directory() -> None:
    """Empty directories should render a clear placeholder and cwd header."""
    blocks = directory_listing("/tmp/project", [], is_cwd=True)

    assert blocks[0]["text"]["text"] == ":open_file_folder: *Current directory:* `/tmp/project`"
    assert blocks[2]["text"]["text"] == "_Directory is empty_"


def test_directory_listing_formats_files_and_directories() -> None:
    """Files and directories should use distinct icons and suffixes."""
    blocks = directory_listing(
        "/tmp/project",
        [("src", True), ("README.md", False)],
    )

    text = blocks[2]["text"]["text"]
    assert ":file_folder: src/" in text
    assert ":page_facing_up: README.md" in text


def test_directory_listing_truncates_after_fifty_entries() -> None:
    """Large listings should be truncated with a count of remaining entries."""
    entries = [(f"file_{index}.txt", False) for index in range(55)]

    blocks = directory_listing("/tmp/project", entries)

    text = blocks[2]["text"]["text"]
    assert ":page_facing_up: file_49.txt" in text
    assert ":page_facing_up: file_50.txt" not in text
    assert "_... and 5 more_" in text


def test_cwd_updated_formats_confirmation_message() -> None:
    """CWD updates should display the new path in code formatting."""
    blocks = cwd_updated("/tmp/project")

    assert blocks == [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":file_folder: Working directory updated to:\n`/tmp/project`",
            },
        }
    ]
