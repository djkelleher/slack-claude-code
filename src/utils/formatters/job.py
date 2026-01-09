"""Job status formatting for parallel and sequential jobs."""

from src.database.models import ParallelJob

from .base import time_ago


def parallel_job_status(job: ParallelJob) -> list[dict]:
    """Format parallel job status."""
    config = job.config
    n_terminals = config.get("n_instances", 0)
    results = job.results or []

    status_text = {
        "pending": ":hourglass: Pending",
        "running": ":arrows_counterclockwise: Running",
        "completed": ":heavy_check_mark: Completed",
        "failed": ":x: Failed",
        "cancelled": ":no_entry: Cancelled",
    }.get(job.status, job.status)

    # Build terminal status list
    terminal_statuses = []
    for i in range(n_terminals):
        if i < len(results):
            result = results[i]
            if result.get("error"):
                terminal_statuses.append(f"Terminal {i + 1}: :x: Failed")
            else:
                terminal_statuses.append(f"Terminal {i + 1}: :heavy_check_mark: Complete")
        elif job.status == "running":
            terminal_statuses.append(f"Terminal {i + 1}: :arrows_counterclockwise: Running...")
        else:
            terminal_statuses.append(f"Terminal {i + 1}: :hourglass: Pending")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":gear: Parallel Analysis ({n_terminals} terminals)",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_text}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(terminal_statuses)},
        },
    ]

    # Add action buttons
    action_elements = []
    if job.status == "completed" and results:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View Results", "emoji": True},
                "action_id": "view_parallel_results",
                "value": str(job.id),
            }
        )
    if job.status in ("pending", "running"):
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                "action_id": "cancel_job",
                "value": str(job.id),
                "style": "danger",
            }
        )

    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements})

    return blocks


def sequential_job_status(job: ParallelJob) -> list[dict]:
    """Format sequential loop job status."""
    config = job.config
    commands = config.get("commands", [])
    loop_count = config.get("loop_count", 1)
    results = job.results or []

    current_loop = len(results) // len(commands) + 1 if commands else 1
    current_cmd = len(results) % len(commands) if commands else 0

    status_text = {
        "pending": ":hourglass: Pending",
        "running": f":arrows_counterclockwise: Running (Loop {current_loop}/{loop_count}, Command {current_cmd + 1}/{len(commands)})",
        "completed": ":heavy_check_mark: Completed",
        "failed": ":x: Failed",
        "cancelled": ":no_entry: Cancelled",
    }.get(job.status, job.status)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":repeat: Sequential Loop ({loop_count}x, {len(commands)} commands)",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_text}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Progress:* {len(results)} / {len(commands) * loop_count} commands completed",
            },
        },
    ]

    # Add action buttons
    if job.status in ("pending", "running"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                        "action_id": "cancel_job",
                        "value": str(job.id),
                        "style": "danger",
                    }
                ],
            }
        )

    return blocks


def job_status_list(jobs: list[ParallelJob]) -> list[dict]:
    """Format list of active jobs."""
    if not jobs:
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":inbox_tray: No active jobs"},
            }
        ]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":gear: Active Jobs", "emoji": True},
        },
        {"type": "divider"},
    ]

    for job in jobs:
        job_type = "Parallel" if job.job_type == "parallel_analysis" else "Sequential"
        status_emoji = ":arrows_counterclockwise:" if job.status == "running" else ":hourglass:"

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Job #{job.id}* {status_emoji} {job_type}\n_{time_ago(job.created_at)}_",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "action_id": "cancel_job",
                    "value": str(job.id),
                    "style": "danger",
                },
            }
        )

    return blocks
