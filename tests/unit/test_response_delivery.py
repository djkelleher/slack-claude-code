"""Unit tests for shared Slack response delivery helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.handlers import response_delivery


@pytest.mark.asyncio
async def test_file_response_posts_detail_button_in_channel_when_thread_missing(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )
    db = SimpleNamespace(store_command_detailed_output=AsyncMock())
    logger = MagicMock()
    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="very large output",
        command_id=7,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=logger,
        db=db,
        post_detail_button=True,
    )

    assert client.chat_postMessage.await_args.kwargs["thread_ts"] is None
    assert client.chat_postMessage.await_args.kwargs["text"] == "📋 Detailed output available"
    db.store_command_detailed_output.assert_awaited_once_with(7, "full output")
    update_blocks = client.chat_update.await_args.kwargs["blocks"]
    assert update_blocks[-1]["type"] == "actions"
    assert update_blocks[-1]["elements"][0]["action_id"] == "copy_command_output"


@pytest.mark.asyncio
async def test_file_response_notifies_when_detail_button_post_fails(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )
    db = SimpleNamespace(store_command_detailed_output=AsyncMock())
    logger = MagicMock()
    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: True)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_file",
        lambda **_kwargs: ([{"type": "section"}], "full output", "response.txt"),
    )
    client.chat_postMessage = AsyncMock(
        side_effect=[RuntimeError("detail button failed"), {"ts": "123.456"}]
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="very large output",
        command_id=7,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=logger,
        db=db,
        notify_on_snippet_failure=True,
        post_detail_button=True,
    )

    assert client.chat_postMessage.await_count == 2
    assert "Could not post detailed output" in client.chat_postMessage.await_args.kwargs["text"]
    db.store_command_detailed_output.assert_awaited_once_with(7, "full output")


@pytest.mark.asyncio
async def test_table_followups_stay_in_channel_when_thread_missing(
    monkeypatch,
) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: False)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_tables",
        lambda **_kwargs: [[{"type": "section"}], [{"type": "table"}]],
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts=None,
        message_ts="111.222",
        prompt="analyze",
        output="table-heavy output",
        command_id=9,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_count == 1
    assert client.chat_postMessage.await_args.kwargs["thread_ts"] is None
    update_blocks = client.chat_update.await_args.kwargs["blocks"]
    assert update_blocks[-1]["type"] == "actions"
    assert update_blocks[-1]["elements"][0]["action_id"] == "copy_command_output"


@pytest.mark.asyncio
async def test_uploads_git_diff_file_when_enabled(monkeypatch) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: False)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_tables",
        lambda **_kwargs: [[{"type": "section"}]],
    )

    status = SimpleNamespace(
        is_clean=False,
        untracked=["new_file.py"],
        summary=lambda: "Branch: main | 1 modified | 1 untracked",
    )

    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        get_status=AsyncMock(return_value=status),
        get_diff=AsyncMock(side_effect=["diff --git a/app.py b/app.py", "(no changes)"]),
    )
    monkeypatch.setattr(response_delivery, "GitService", lambda: git_service)

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts="111.222",
        message_ts="333.444",
        prompt="analyze",
        output="done",
        command_id=9,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=MagicMock(),
        working_directory="/repo",
        upload_git_diff=True,
    )

    client.files_upload_v2.assert_awaited_once()
    upload_kwargs = client.files_upload_v2.await_args.kwargs
    assert upload_kwargs["channel"] == "C123"
    assert upload_kwargs["thread_ts"] == "111.222"
    assert upload_kwargs["filename"] == "git-diff-command-9.diff"
    assert "diff --git a/app.py b/app.py" in upload_kwargs["content"]
    assert "new_file.py" in upload_kwargs["content"]


@pytest.mark.asyncio
async def test_skips_git_diff_upload_when_repo_is_clean(monkeypatch) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: False)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_tables",
        lambda **_kwargs: [[{"type": "section"}]],
    )

    status = SimpleNamespace(is_clean=True, untracked=[], summary=lambda: "Branch: main | (clean)")
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        get_status=AsyncMock(return_value=status),
        get_diff=AsyncMock(),
    )
    monkeypatch.setattr(response_delivery, "GitService", lambda: git_service)

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts="111.222",
        message_ts="333.444",
        prompt="analyze",
        output="done",
        command_id=10,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=MagicMock(),
        working_directory="/repo",
        upload_git_diff=True,
    )

    client.files_upload_v2.assert_not_awaited()


@pytest.mark.asyncio
async def test_uploads_git_activity_file_when_present(monkeypatch) -> None:
    client = SimpleNamespace(
        chat_update=AsyncMock(),
        chat_postMessage=AsyncMock(),
        files_upload_v2=AsyncMock(),
    )

    monkeypatch.setattr(response_delivery, "should_attach_file", lambda _output: False)
    monkeypatch.setattr(
        response_delivery,
        "command_response_with_tables",
        lambda **_kwargs: [[{"type": "section"}]],
    )

    await response_delivery.deliver_command_response(
        client=client,
        channel_id="C123",
        thread_ts="111.222",
        message_ts="333.444",
        prompt="analyze",
        output="done",
        command_id=11,
        duration_ms=1000,
        cost_usd=0.1,
        is_error=False,
        logger=MagicMock(),
        git_tool_events=[
            {
                "kind": "shell",
                "tool_id": "tool-1",
                "tool_name": "run_command",
                "command": "git commit -m test",
                "result": "[main abc123] test",
                "is_error": False,
                "duration_ms": 120,
            }
        ],
        upload_git_activity=True,
    )

    client.files_upload_v2.assert_awaited_once()
    upload_kwargs = client.files_upload_v2.await_args.kwargs
    assert upload_kwargs["channel"] == "C123"
    assert upload_kwargs["thread_ts"] == "111.222"
    assert upload_kwargs["filename"] == "git-activity-command-11.txt"
    assert "git commit -m test" in upload_kwargs["content"]
    assert "[main abc123] test" in upload_kwargs["content"]
