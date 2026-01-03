import re
from datetime import datetime
from typing import Optional
from src.database.models import ParallelJob


class SlackFormatter:
    """Formats messages for Slack using Block Kit."""

    MAX_TEXT_LENGTH = 2900  # Leave room for formatting
    FILE_THRESHOLD = 2000  # Attach as file if longer than this

    @classmethod
    def command_response(
        cls,
        prompt: str,
        output: str,
        command_id: int,
        duration_ms: Optional[int] = None,
        cost_usd: Optional[float] = None,
        is_error: bool = False,
    ) -> list[dict]:
        """Format a command response."""
        # Truncate output if needed
        if len(output) > cls.MAX_TEXT_LENGTH:
            output = output[: cls.MAX_TEXT_LENGTH - 50] + "\n\n... (output truncated)"

        blocks = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"> {cls._escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": output or "_No output_"},
            },
        ]

        # Add footer with metadata
        footer_parts = []
        if duration_ms:
            footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
        if cost_usd:
            footer_parts.append(f":moneybag: ${cost_usd:.4f}")
        footer_parts.append(f":memo: History #{command_id}")

        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
            }
        )

        return blocks

    @classmethod
    def processing_message(cls, prompt: str) -> list[dict]:
        """Format a 'processing' placeholder message."""
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":hourglass_flowing_sand: *Processing...*\n> {cls._escape_markdown(prompt[:100])}{'...' if len(prompt) > 100 else ''}",
                },
            }
        ]

    @classmethod
    def streaming_update(cls, prompt: str, current_output: str, is_complete: bool = False) -> list[dict]:
        """Format a streaming update message."""
        status = ":white_check_mark: Complete" if is_complete else ":arrows_counterclockwise: Streaming..."

        if len(current_output) > cls.MAX_TEXT_LENGTH:
            current_output = current_output[-cls.MAX_TEXT_LENGTH + 50:]
            current_output = "... (earlier output truncated)\n\n" + current_output

        return [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{status}\n> {cls._escape_markdown(prompt[:100])}{'...' if len(prompt) > 100 else ''}",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": current_output or "_Waiting for response..._"},
            },
        ]

    @classmethod
    def parallel_job_status(cls, job: ParallelJob) -> list[dict]:
        """Format parallel job status."""
        config = job.config
        n_terminals = config.get("n_instances", 0)
        results = job.results or []

        status_text = {
            "pending": ":hourglass: Pending",
            "running": ":arrows_counterclockwise: Running",
            "completed": ":white_check_mark: Completed",
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
                    terminal_statuses.append(f"Terminal {i + 1}: :white_check_mark: Complete")
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

    @classmethod
    def sequential_job_status(cls, job: ParallelJob) -> list[dict]:
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
            "completed": ":white_check_mark: Completed",
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

    @classmethod
    def _sanitize_error(cls, error: str) -> str:
        """Sanitize error message to remove sensitive information."""
        # Redact home directory paths
        sanitized = re.sub(r'/home/[^/\s]+', '/home/***', error)
        # Redact common sensitive values
        sanitized = re.sub(
            r'(password|secret|token|key|api_key|apikey|auth)=[^\s&"\']+',
            r'\1=***',
            sanitized,
            flags=re.IGNORECASE,
        )
        # Redact environment variable values that might contain secrets
        sanitized = re.sub(
            r'(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_SIGNING_SECRET|DATABASE_PATH)=[^\s]+',
            r'\1=***',
            sanitized,
            flags=re.IGNORECASE,
        )
        return sanitized[:2500]

    @classmethod
    def error_message(cls, error: str) -> list[dict]:
        """Format an error message with sensitive information redacted."""
        sanitized = cls._sanitize_error(error)
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *Error*\n```{cls._escape_markdown(sanitized)}```",
                },
            }
        ]

    @classmethod
    def cwd_updated(cls, new_cwd: str) -> list[dict]:
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

    @classmethod
    def job_status_list(cls, jobs: list[ParallelJob]) -> list[dict]:
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
                        "text": f"*Job #{job.id}* {status_emoji} {job_type}\n_{cls._time_ago(job.created_at)}_",
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

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """Escape special Slack markdown characters."""
        # Only escape what's necessary for Slack mrkdwn
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        return text

    @staticmethod
    def _time_ago(dt: datetime) -> str:
        """Format a datetime as 'X time ago'."""
        now = datetime.now()
        diff = now - dt

        seconds = diff.total_seconds()
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            mins = int(seconds / 60)
            return f"{mins} min{'s' if mins != 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"

    @classmethod
    def should_attach_file(cls, output: str) -> bool:
        """Check if output is large enough to warrant a file attachment."""
        return len(output) > cls.FILE_THRESHOLD

    @classmethod
    def command_response_with_file(
        cls,
        prompt: str,
        output: str,
        command_id: int,
        duration_ms: Optional[int] = None,
        cost_usd: Optional[float] = None,
        is_error: bool = False,
    ) -> tuple[list[dict], str, str]:
        """Format response with file attachment for large outputs.

        Returns:
            Tuple of (blocks, file_content, file_title)
        """
        # Extract a preview (first meaningful content)
        lines = output.strip().split("\n")
        preview_lines = []
        char_count = 0
        for line in lines:
            if char_count + len(line) > 500:
                break
            preview_lines.append(line)
            char_count += len(line)

        preview = "\n".join(preview_lines)
        if len(output) > len(preview):
            preview += "\n\n_... (see attached file for full response)_"

        blocks = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"> {cls._escape_markdown(prompt[:200])}{'...' if len(prompt) > 200 else ''}",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": preview or "_No output_"},
            },
        ]

        # Add footer with metadata
        footer_parts = [f":page_facing_up: Full response attached ({len(output):,} chars)"]
        if duration_ms:
            footer_parts.append(f":stopwatch: {duration_ms / 1000:.1f}s")
        if cost_usd:
            footer_parts.append(f":moneybag: ${cost_usd:.4f}")
        footer_parts.append(f":memo: History #{command_id}")

        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " | ".join(footer_parts)}],
            }
        )

        file_title = f"claude_response_{command_id}.txt"
        return blocks, output, file_title

    @classmethod
    def queue_status(cls, pending: list, running) -> list[dict]:
        """Format queue status for /qv command."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":inbox_tray: Command Queue",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        if running:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":arrow_forward: *Running:* #{running.id}\n> {cls._escape_markdown(running.prompt[:100])}{'...' if len(running.prompt) > 100 else ''}",
                    },
                }
            )
            blocks.append({"type": "divider"})

        if not pending:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Queue is empty_"},
                }
            )
        else:
            for item in pending[:10]:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*#{item.id}* (pos {item.position})\n> {cls._escape_markdown(item.prompt[:100])}{'...' if len(item.prompt) > 100 else ''}",
                        },
                    }
                )

            if len(pending) > 10:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"_... and {len(pending) - 10} more_"}
                        ],
                    }
                )

        return blocks

    @classmethod
    def queue_item_running(cls, item) -> list[dict]:
        """Format running queue item status."""
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":arrow_forward: *Processing Queue Item #{item.id}*\n> {cls._escape_markdown(item.prompt[:200])}{'...' if len(item.prompt) > 200 else ''}",
                },
            },
        ]

    @classmethod
    def queue_item_complete(cls, item, result) -> list[dict]:
        """Format completed queue item."""
        status = ":white_check_mark:" if result.success else ":x:"
        output = result.output or result.error or "No output"
        if len(output) > 2500:
            output = output[:2500] + "\n\n... (truncated)"

        return [
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"{status} Queue Item #{item.id}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"> {cls._escape_markdown(item.prompt[:100])}{'...' if len(item.prompt) > 100 else ''}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": output},
            },
        ]

    @classmethod
    def directory_listing(
        cls, path: str, entries: list[tuple[str, bool]], is_cwd: bool = False
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
