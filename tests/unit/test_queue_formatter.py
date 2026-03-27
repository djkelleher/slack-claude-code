"""Unit tests for queue formatter helpers."""

from datetime import datetime
from types import SimpleNamespace

from src.config import config
from src.utils.formatters import queue as queue_fmt


def test_queue_status_renders_running_scheduled_and_pending_sections() -> None:
    """Queue status should include running, scheduled, and pending summaries."""
    running = [
        SimpleNamespace(
            id=1,
            prompt="run <lint> & deploy",
            parallel_group_id="grp-1",
            parallel_limit=2,
            automation_meta=None,
        )
    ]
    pending = [
        SimpleNamespace(
            id=2,
            prompt="follow up",
            parallel_group_id=None,
            parallel_limit=None,
            automation_meta=None,
        )
    ]
    scheduled = [
        SimpleNamespace(id=501, action="resume", execute_at=datetime(2026, 3, 21, 15, 30)),
        SimpleNamespace(id=None, action="pause", execute_at=datetime(2026, 3, 21, 16, 0)),
    ]

    blocks = queue_fmt.queue_status(pending=pending, running=running, scheduled_events=scheduled)

    assert "parallel `grp-1` (max 2)" in blocks[2]["text"]["text"]
    assert "&lt;lint&gt; &amp; deploy" in blocks[2]["text"]["text"]
    assert "*Scheduled Controls:*" in blocks[4]["text"]["text"]
    assert "#501" in blocks[4]["text"]["text"]
    assert "2026-03-21 15:30 UTC" in blocks[4]["text"]["text"]
    assert "*#2* (pos 1)" in blocks[6]["text"]["text"]


def test_queue_status_shows_empty_pending_and_overflow_notices() -> None:
    """Empty and overflow queue states should render clear notices."""
    pending = [
        SimpleNamespace(
            id=index,
            prompt=f"task {index}",
            parallel_group_id=None,
            parallel_limit=None,
            automation_meta=None,
        )
        for index in range(12)
    ]
    scheduled = [
        SimpleNamespace(id=None, action=f"action-{index}", execute_at=datetime(2026, 3, 21, 12, 0))
        for index in range(7)
    ]

    blocks = queue_fmt.queue_status(pending=pending, running=None, scheduled_events=scheduled)

    overflow_texts = [
        block["elements"][0]["text"] for block in blocks if block["type"] == "context"
    ]
    assert "_... and 2 more_" in overflow_texts
    assert blocks[-2]["text"]["text"].startswith("*#9*")
    assert blocks[-1]["elements"][0]["text"] == "_... and 2 more_"

    empty_blocks = queue_fmt.queue_status(pending=[], running=None)
    assert empty_blocks[-1]["text"]["text"] == "_Queue is empty_"


def test_queue_status_formats_pending_parallel_suffix() -> None:
    """Pending parallel items should display their concurrency limit."""
    pending = [
        SimpleNamespace(
            id=21,
            prompt="parallel task",
            parallel_group_id="grp-2",
            parallel_limit=None,
            automation_meta=None,
        )
    ]

    blocks = queue_fmt.queue_status(pending=pending, running=None)

    assert "parallel max all" in blocks[-1]["text"]["text"]


def test_queue_status_includes_usage_limit_prefixes() -> None:
    """Queue status should show attached usage-limit labels in item previews."""
    pending = [
        SimpleNamespace(
            id=31,
            prompt="limited task",
            parallel_group_id=None,
            parallel_limit=None,
            automation_meta={
                "usage_limits": [
                    {"id": "limit-1", "percent": 2.5, "window": "5h", "action": "pause"}
                ]
            },
        )
    ]

    blocks = queue_fmt.queue_status(pending=pending, running=None)

    assert "limit 2.5% 5h pause" in blocks[-1]["text"]["text"]


def test_queue_item_running_and_complete_render_status_and_truncation() -> None:
    """Queue item formatters should keep running previews Slack-safe and truncate long output."""
    item = SimpleNamespace(id=9, prompt="process " + ("x" * 4000), automation_meta=None)
    success = SimpleNamespace(success=True, output="done", error=None)
    failure = SimpleNamespace(success=False, output="", error="E" * 2600)

    running_blocks = queue_fmt.queue_item_running(item, "3")
    success_blocks = queue_fmt.queue_item_complete(item, success)
    failure_blocks = queue_fmt.queue_item_complete(item, failure)

    running_text = running_blocks[0]["text"]["text"]
    assert "Processing queue item 3" in running_text
    assert len(running_text) <= config.SLACK_BLOCK_TEXT_LIMIT
    assert "..." in running_text
    assert success_blocks[0]["elements"][0]["text"] == ":heavy_check_mark: Queue Item #9"
    assert success_blocks[3]["text"]["text"] == "done"
    assert failure_blocks[0]["elements"][0]["text"] == ":x: Queue Item #9"
    assert failure_blocks[3]["text"]["text"].endswith("... (truncated)")


def test_queue_scope_overview_handles_empty_and_truncates_scope_list() -> None:
    """Scope overview should render summaries, previews, and overflow notices."""
    empty_blocks = queue_fmt.queue_scope_overview([])
    assert empty_blocks[-1]["text"]["text"] == "_No queue activity found in this channel_"

    scopes = [
        {
            "label": f"Thread <{index}>",
            "state": "running" if index == 0 else "paused",
            "running_count": index,
            "pending_count": index + 1,
            "scheduled_count": 1 if index == 0 else 0,
            "preview": "next <task>" if index == 0 else "",
        }
        for index in range(17)
    ]

    blocks = queue_fmt.queue_scope_overview(scopes)

    assert "*Thread &lt;0&gt;*" in blocks[2]["text"]["text"]
    assert "*Scheduled:* 1" in blocks[2]["text"]["text"]
    assert "&lt;task&gt;" in blocks[2]["text"]["text"]
    assert blocks[-1]["elements"][0]["text"] == "_... and 2 more_"
