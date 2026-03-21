"""Unit tests for job status formatters."""

from datetime import datetime, timezone

from src.database.models import ParallelJob
from src.utils.formatters import job as job_fmt


def test_parallel_job_status_shows_terminal_results_and_actions() -> None:
    """Completed parallel jobs should show per-terminal outcomes and actions."""
    job = ParallelJob(
        id=42,
        job_type="parallel_analysis",
        status="completed",
        config={"n_instances": 3},
        results=[{"output": "ok"}, {"error": "boom"}],
    )

    blocks = job_fmt.parallel_job_status(job)

    assert blocks[1]["elements"][0]["text"] == ":heavy_check_mark: Completed"
    terminal_text = blocks[3]["text"]["text"]
    assert "Terminal 1: :heavy_check_mark: Complete" in terminal_text
    assert "Terminal 2: :x: Failed" in terminal_text
    assert "Terminal 3: :hourglass: Pending" in terminal_text
    action_ids = [element["action_id"] for element in blocks[-1]["elements"]]
    assert action_ids == ["view_parallel_results"]


def test_sequential_job_status_shows_progress_and_cancel_while_running() -> None:
    """Running sequential jobs should describe loop progress and offer cancel."""
    job = ParallelJob(
        id=7,
        job_type="sequential_loop",
        status="running",
        config={"commands": ["a", "b"], "loop_count": 3},
        results=[{"output": "ok"}, {"output": "ok"}, {"output": "ok"}],
    )

    blocks = job_fmt.sequential_job_status(job)

    assert "Loop 2/3, Command 2/2" in blocks[1]["elements"][0]["text"]
    assert "*Progress:* 3 / 6 commands completed" == blocks[3]["text"]["text"]
    assert blocks[4]["elements"][0]["action_id"] == "cancel_job"


def test_job_status_list_handles_empty_and_formats_entries(monkeypatch) -> None:
    """Job lists should render an empty state and formatted entries."""
    assert job_fmt.job_status_list([]) == [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":inbox_tray: No active jobs"},
        }
    ]

    monkeypatch.setattr(job_fmt, "time_ago", lambda _created_at: "2 mins ago")
    jobs = [
        ParallelJob(
            id=1,
            job_type="parallel_analysis",
            status="running",
            created_at=datetime.now(timezone.utc),
        ),
        ParallelJob(
            id=2,
            job_type="sequential_loop",
            status="pending",
            created_at=datetime.now(timezone.utc),
        ),
    ]

    blocks = job_fmt.job_status_list(jobs)

    assert blocks[2]["text"]["text"] == "*Job #1* :arrows_counterclockwise: Parallel\n_2 mins ago_"
    assert blocks[3]["text"]["text"] == "*Job #2* :hourglass: Sequential\n_2 mins ago_"
    assert blocks[2]["accessory"]["action_id"] == "cancel_job"
