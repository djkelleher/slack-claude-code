"""Slack message formatting - facade for formatters module.

This module provides backward compatibility through the SlackFormatter class.
New code should import directly from src.utils.formatters.
"""

from datetime import datetime
from typing import Optional

from src.database.models import ParallelJob

from .formatters import (
    FILE_THRESHOLD,
    MAX_TEXT_LENGTH,
    command_response,
    command_response_with_file,
    cwd_updated,
    directory_listing,
    error_message,
    escape_markdown,
    job_status_list,
    parallel_job_status,
    processing_message,
    queue_item_complete,
    queue_item_running,
    queue_status,
    sanitize_error,
    sequential_job_status,
    should_attach_file,
    streaming_update,
    time_ago,
)


class SlackFormatter:
    """Formats messages for Slack using Block Kit.

    This class provides backward compatibility. New code should import
    functions directly from src.utils.formatters.
    """

    MAX_TEXT_LENGTH = MAX_TEXT_LENGTH
    FILE_THRESHOLD = FILE_THRESHOLD

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
        return command_response(prompt, output, command_id, duration_ms, cost_usd, is_error)

    @classmethod
    def processing_message(cls, prompt: str) -> list[dict]:
        return processing_message(prompt)

    @classmethod
    def streaming_update(cls, prompt: str, current_output: str, is_complete: bool = False) -> list[dict]:
        return streaming_update(prompt, current_output, is_complete)

    @classmethod
    def parallel_job_status(cls, job: ParallelJob) -> list[dict]:
        return parallel_job_status(job)

    @classmethod
    def sequential_job_status(cls, job: ParallelJob) -> list[dict]:
        return sequential_job_status(job)

    @classmethod
    def _sanitize_error(cls, error: str) -> str:
        return sanitize_error(error)

    @classmethod
    def error_message(cls, error: str) -> list[dict]:
        return error_message(error)

    @classmethod
    def cwd_updated(cls, new_cwd: str) -> list[dict]:
        return cwd_updated(new_cwd)

    @classmethod
    def job_status_list(cls, jobs: list[ParallelJob]) -> list[dict]:
        return job_status_list(jobs)

    @staticmethod
    def _escape_markdown(text: str) -> str:
        return escape_markdown(text)

    @staticmethod
    def _time_ago(dt: datetime) -> str:
        return time_ago(dt)

    @classmethod
    def should_attach_file(cls, output: str) -> bool:
        return should_attach_file(output)

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
        return command_response_with_file(prompt, output, command_id, duration_ms, cost_usd, is_error)

    @classmethod
    def queue_status(cls, pending: list, running) -> list[dict]:
        return queue_status(pending, running)

    @classmethod
    def queue_item_running(cls, item) -> list[dict]:
        return queue_item_running(item)

    @classmethod
    def queue_item_complete(cls, item, result) -> list[dict]:
        return queue_item_complete(item, result)

    @classmethod
    def directory_listing(
        cls, path: str, entries: list[tuple[str, bool]], is_cwd: bool = False
    ) -> list[dict]:
        return directory_listing(path, entries, is_cwd)
