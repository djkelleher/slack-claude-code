"""Unit tests for database models."""

import json
from datetime import datetime

import pytest

from src.database.models import (
    CommandHistory,
    GitCheckpoint,
    NotificationSettings,
    ParallelJob,
    QueueItem,
    Session,
    UploadedFile,
)


class TestSession:
    """Tests for Session model."""

    def test_from_row_new_schema(self):
        """from_row handles new schema with model column."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            "1234567890.123456",  # thread_ts
            "/home/user",  # working_directory
            "session-abc123",  # claude_session_id
            "plan",  # permission_mode
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # last_active
            "opus",  # model (at position 8)
        )

        session = Session.from_row(row)

        assert session.id == 1
        assert session.channel_id == "C123ABC"
        assert session.thread_ts == "1234567890.123456"
        assert session.working_directory == "/home/user"
        assert session.claude_session_id == "session-abc123"
        assert session.permission_mode == "plan"
        assert session.model == "opus"
        assert session.created_at == datetime.fromisoformat("2024-01-15T10:30:00")
        assert session.last_active == datetime.fromisoformat("2024-01-15T11:00:00")

    def test_from_row_old_schema(self):
        """from_row handles old schema without model column."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            None,  # thread_ts
            "~",  # working_directory
            None,  # claude_session_id
            None,  # permission_mode
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # last_active
        )

        session = Session.from_row(row)

        assert session.id == 1
        assert session.channel_id == "C123ABC"
        assert session.thread_ts is None
        assert session.model is None

    def test_from_row_handles_null_dates(self):
        """from_row handles null date values."""
        row = (1, "C123", None, "~", None, None, None, None, None)

        session = Session.from_row(row)

        assert session.id == 1
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_active, datetime)

    def test_is_thread_session_true(self):
        """is_thread_session returns True for thread sessions."""
        session = Session(channel_id="C123", thread_ts="1234567890.123456")
        assert session.is_thread_session() is True

    def test_is_thread_session_false(self):
        """is_thread_session returns False for channel sessions."""
        session = Session(channel_id="C123", thread_ts=None)
        assert session.is_thread_session() is False

    def test_session_display_name_thread(self):
        """session_display_name formats thread sessions correctly."""
        session = Session(channel_id="C123ABC", thread_ts="1234567890.123456")
        assert session.session_display_name() == "C123ABC (Thread: 1234567890.123456)"

    def test_session_display_name_channel(self):
        """session_display_name formats channel sessions correctly."""
        session = Session(channel_id="C123ABC", thread_ts=None)
        assert session.session_display_name() == "C123ABC (Channel)"


class TestCommandHistory:
    """Tests for CommandHistory model."""

    def test_from_row_complete(self):
        """from_row parses complete row correctly."""
        row = (
            42,  # id
            1,  # session_id
            "analyze code",  # command
            "Analysis complete",  # output
            "completed",  # status
            None,  # error_message
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:35:00",  # completed_at
        )

        cmd = CommandHistory.from_row(row)

        assert cmd.id == 42
        assert cmd.session_id == 1
        assert cmd.command == "analyze code"
        assert cmd.output == "Analysis complete"
        assert cmd.status == "completed"
        assert cmd.error_message is None
        assert cmd.completed_at == datetime.fromisoformat("2024-01-15T10:35:00")

    def test_from_row_failed(self):
        """from_row parses failed command with error."""
        row = (
            1,
            1,
            "bad command",
            None,
            "failed",
            "Something went wrong",
            "2024-01-15T10:30:00",
            "2024-01-15T10:31:00",
        )

        cmd = CommandHistory.from_row(row)

        assert cmd.status == "failed"
        assert cmd.error_message == "Something went wrong"

    def test_default_values(self):
        """CommandHistory has correct defaults."""
        cmd = CommandHistory()

        assert cmd.id is None
        assert cmd.session_id == 0
        assert cmd.command == ""
        assert cmd.output is None
        assert cmd.status == "pending"
        assert cmd.error_message is None
        assert cmd.completed_at is None


class TestParallelJob:
    """Tests for ParallelJob model."""

    def test_from_row_complete(self):
        """from_row parses complete job correctly."""
        config_json = json.dumps({"n_instances": 3, "commands": ["cmd1", "cmd2"]})
        results_json = json.dumps([{"output": "result1"}, {"output": "result2"}])

        row = (
            1,  # id
            5,  # session_id
            "C123",  # channel_id
            "parallel_analysis",  # job_type
            "completed",  # status
            config_json,  # config
            results_json,  # results
            "aggregated output",  # aggregation_output
            "1234567890.123456",  # message_ts
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:35:00",  # completed_at
        )

        job = ParallelJob.from_row(row)

        assert job.id == 1
        assert job.session_id == 5
        assert job.channel_id == "C123"
        assert job.job_type == "parallel_analysis"
        assert job.status == "completed"
        assert job.config == {"n_instances": 3, "commands": ["cmd1", "cmd2"]}
        assert job.results == [{"output": "result1"}, {"output": "result2"}]
        assert job.aggregation_output == "aggregated output"
        assert job.message_ts == "1234567890.123456"

    def test_from_row_handles_null_json(self):
        """from_row handles null JSON fields."""
        row = (1, 1, "C123", "test", "pending", None, None, None, None, None, None)

        job = ParallelJob.from_row(row)

        assert job.config == {}
        assert job.results == []


class TestQueueItem:
    """Tests for QueueItem model."""

    def test_from_row_complete(self):
        """from_row parses complete queue item correctly."""
        row = (
            10,  # id
            1,  # session_id
            "C123",  # channel_id
            "analyze this code",  # prompt
            "running",  # status
            "partial output",  # output
            None,  # error_message
            5,  # position
            "1234567890.123456",  # message_ts
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:31:00",  # started_at
            None,  # completed_at
        )

        item = QueueItem.from_row(row)

        assert item.id == 10
        assert item.prompt == "analyze this code"
        assert item.status == "running"
        assert item.position == 5
        assert item.started_at == datetime.fromisoformat("2024-01-15T10:31:00")
        assert item.completed_at is None

    def test_default_values(self):
        """QueueItem has correct defaults."""
        item = QueueItem()

        assert item.id is None
        assert item.status == "pending"
        assert item.position == 0
        assert item.output is None


class TestUploadedFile:
    """Tests for UploadedFile model."""

    def test_from_row_complete(self):
        """from_row parses complete uploaded file correctly."""
        row = (
            1,  # id
            5,  # session_id
            "F123ABC",  # slack_file_id
            "report.pdf",  # filename
            "application/pdf",  # mimetype
            102400,  # size
            "/tmp/uploads/report.pdf",  # local_path
            "2024-01-15T10:30:00",  # uploaded_at
            "2024-01-15T11:00:00",  # last_referenced
        )

        file = UploadedFile.from_row(row)

        assert file.id == 1
        assert file.slack_file_id == "F123ABC"
        assert file.filename == "report.pdf"
        assert file.mimetype == "application/pdf"
        assert file.size == 102400
        assert file.local_path == "/tmp/uploads/report.pdf"


class TestGitCheckpoint:
    """Tests for GitCheckpoint model."""

    def test_from_row_complete(self):
        """from_row parses complete checkpoint correctly."""
        row = (
            1,  # id
            5,  # session_id
            "C123",  # channel_id
            "before-refactor",  # name
            "stash@{0}",  # stash_ref
            "checkpoint: before-refactor",  # stash_message
            "Saving state before major refactor",  # description
            "2024-01-15T10:30:00",  # created_at
            0,  # is_auto (False)
        )

        checkpoint = GitCheckpoint.from_row(row)

        assert checkpoint.id == 1
        assert checkpoint.name == "before-refactor"
        assert checkpoint.stash_ref == "stash@{0}"
        assert checkpoint.description == "Saving state before major refactor"
        assert checkpoint.is_auto is False

    def test_from_row_auto_checkpoint(self):
        """from_row handles auto checkpoints correctly."""
        row = (1, 1, "C123", "auto-save", "stash@{1}", None, None, "2024-01-15T10:30:00", 1)

        checkpoint = GitCheckpoint.from_row(row)

        assert checkpoint.is_auto is True


class TestNotificationSettings:
    """Tests for NotificationSettings model."""

    def test_from_row_complete(self):
        """from_row parses complete settings correctly."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            1,  # notify_on_completion (True)
            0,  # notify_on_permission (False)
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # updated_at
        )

        settings = NotificationSettings.from_row(row)

        assert settings.id == 1
        assert settings.channel_id == "C123ABC"
        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is False

    def test_default_factory(self):
        """default creates settings with all notifications enabled."""
        settings = NotificationSettings.default("C123ABC")

        assert settings.channel_id == "C123ABC"
        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is True
        assert settings.id is None

    def test_default_values(self):
        """NotificationSettings has correct defaults."""
        settings = NotificationSettings()

        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is True
