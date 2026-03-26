"""Unit tests for app-level helpers."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import (
    _application_data_dir,
    _event_dedupe_key,
    _extract_single_prompt_mode_directive,
    _extract_structured_queue_plan_from_uploaded_files,
    _handle_typed_model_command,
    _is_duplicate_event,
    _post_message_processing_error,
    _queue_structured_plan_message,
    _restore_pending_queue_processors,
    _route_claude_message_to_active_execution_or_queue,
    _route_codex_message_to_active_turn_or_queue,
    _slack_uploads_dir,
    _strip_leading_slack_mention,
    configure_logging,
    slack_api_with_retry,
)


class TestSlackApiRetry:
    """Tests for Slack API retry helper."""

    @pytest.mark.asyncio
    async def test_slack_api_with_retry_propagates_cancellation_immediately(self):
        """CancelledError should never be retried."""
        call_count = 0

        async def failing_call():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await slack_api_with_retry(failing_call, max_retries=3, base_delay=0)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_slack_api_with_retry_rejects_non_positive_retry_count(self):
        """Retry helper should validate that at least one attempt is allowed."""

        async def successful_call():
            return "ok"

        with pytest.raises(ValueError, match="at least 1"):
            await slack_api_with_retry(successful_call, max_retries=0)


class TestConfigureLogging:
    """Tests for logger sink configuration."""

    def test_configure_logging_writes_log_file_to_database_directory(self, tmp_path):
        """Log file should live next to the configured database with 3-day retention."""
        db_path = tmp_path / "data" / "slack_claude.db"
        expected_log_path = db_path.parent / "slack_claude.log"

        with patch("src.app.config.DATABASE_PATH", str(db_path)):
            with patch("src.app.logger.remove") as mock_remove:
                with patch("src.app.logger.add") as mock_add:
                    configure_logging()

        mock_remove.assert_called_once_with()
        assert mock_add.call_count == 2
        assert mock_add.call_args_list[0].args[0] is sys.stderr

        file_sink_call = mock_add.call_args_list[1]
        assert file_sink_call.args[0] == expected_log_path
        assert file_sink_call.kwargs["retention"] == "3 days"
        assert file_sink_call.kwargs["rotation"] == "00:00"

        assert expected_log_path.parent.exists()

    def test_slack_uploads_dir_lives_under_application_data_dir(self, tmp_path):
        """Slack uploads should be stored under the app data directory."""
        db_path = tmp_path / "data" / "slack_claude.db"
        expected_data_dir = db_path.parent

        with patch("src.app.config.DATABASE_PATH", str(db_path)):
            assert _application_data_dir() == expected_data_dir
            assert _slack_uploads_dir() == expected_data_dir / "slack_uploads"


class TestEventHelpers:
    """Tests for Slack message normalization and dedupe helpers."""

    def test_strip_leading_slack_mention(self):
        """Leading bot mention should be stripped while preserving prompt text."""
        assert _strip_leading_slack_mention("<@U123> run tests") == "run tests"
        assert _strip_leading_slack_mention("  <@U123>   run tests  ") == "run tests"
        assert _strip_leading_slack_mention("run tests") == "run tests"

    def test_event_dedupe_key_uses_channel_ts_and_user(self):
        """Dedupe key should be stable across message/app_mention payloads."""
        event = {"channel": "C123", "ts": "111.222", "user": "U999"}
        assert _event_dedupe_key(event) == "C123:111.222:U999"

    def test_duplicate_event_detection_with_ttl(self):
        """Duplicate events inside TTL should be ignored; later events should pass."""
        seen: dict[str, float] = {}
        event = {"channel": "C123", "ts": "111.222", "user": "U999"}

        assert (
            _is_duplicate_event(event, seen, now_monotonic=100.0, ttl_seconds=30.0)
            is False
        )
        assert (
            _is_duplicate_event(event, seen, now_monotonic=105.0, ttl_seconds=30.0)
            is True
        )
        assert (
            _is_duplicate_event(event, seen, now_monotonic=131.0, ttl_seconds=30.0)
            is False
        )

    def test_extract_single_prompt_mode_directive_strips_wrapper(self):
        prompt, mode = _extract_single_prompt_mode_directive(
            "(mode: plan)\nCreate migration plan\n(end)"
        )
        assert prompt == "Create migration plan"
        assert mode == "plan"

    def test_extract_single_prompt_mode_directive_keeps_semicolon_subdirectives(self):
        prompt, mode = _extract_single_prompt_mode_directive(
            "(mode: splan: cs46h, g54h; sandbox read-only)\nCreate migration plan"
        )
        assert prompt == "Create migration plan"
        assert mode == "splan: cs46h, g54h; sandbox read-only"


class TestUploadedStructuredQueuePlanDetection:
    """Tests for structured queue-plan extraction from uploaded files."""

    def test_extracts_queue_plan_from_text_snippet_file(self, tmp_path):
        plan_text = "first task\n***\nsecond task"
        snippet_path = tmp_path / "snippet"
        snippet_path.write_text(plan_text, encoding="utf-8")
        uploaded = SimpleNamespace(
            filename="snippet",
            mimetype="",
            local_path=str(snippet_path),
        )

        extracted = _extract_structured_queue_plan_from_uploaded_files(
            [uploaded], logger=MagicMock()
        )

        assert extracted == plan_text

    def test_returns_none_when_uploaded_text_has_no_queue_markers(self, tmp_path):
        text_path = tmp_path / "notes.txt"
        text_path.write_text("just a note", encoding="utf-8")
        uploaded = SimpleNamespace(
            filename="notes.txt",
            mimetype="text/plain",
            local_path=str(text_path),
        )

        extracted = _extract_structured_queue_plan_from_uploaded_files(
            [uploaded], logger=MagicMock()
        )

        assert extracted is None


class TestTypedModelCommand:
    """Tests for redirecting typed /model messages to the slash command."""

    @pytest.mark.asyncio
    async def test_typed_model_command_points_to_selector(self):
        """Typed /model guidance should describe the current select-based UI."""
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        await _handle_typed_model_command(
            client,
            channel_id="C123",
            thread_ts="123.456",
            message_ts="123.456",
        )

        client.chat_postMessage.assert_awaited_once()
        kwargs = client.chat_postMessage.await_args.kwargs
        assert (
            kwargs["text"] == "Use `/model` slash command to open the model selector."
        )
        assert "open the model selector" in kwargs["blocks"][0]["text"]["text"]


class TestUnexpectedMessageErrorReporting:
    """Tests for the generic message-processing failure notifier."""

    @pytest.mark.asyncio
    async def test_post_message_processing_error_truncates_slack_payload(self):
        """Unexpected error reporter should cap Slack text/block length."""
        client = SimpleNamespace(chat_postMessage=AsyncMock())
        long_error = "x" * 4000

        await _post_message_processing_error(
            client=client,
            channel_id="C123",
            thread_ts="123.456",
            error_text=long_error,
        )

        client.chat_postMessage.assert_awaited_once()
        kwargs = client.chat_postMessage.await_args.kwargs
        assert len(kwargs["text"]) < 300
        assert kwargs["text"].endswith("...")
        block_text = kwargs["blocks"][0]["text"]["text"]
        assert len(block_text) < 2000
        assert block_text.endswith("...```")


class TestCodexActiveTurnRouting:
    """Tests for active-turn steer and queue fallback behavior."""

    @pytest.mark.asyncio
    async def test_routes_to_active_turn_when_steer_succeeds(self):
        """Active Codex turn should consume follow-up message via steer."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(
                        success=True, turn_id="turn-123", error=None
                    )
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=10)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_codex_message_to_active_turn_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.add_to_queue.assert_not_called()
        deps.db.update_command_status.assert_any_await(
            10,
            "completed",
            output="Routed to active Codex turn via turn/steer. turn_id=turn-123",
        )

    @pytest.mark.asyncio
    async def test_queues_message_when_steer_fails(self):
        """Steer failure should auto-queue and start queue processor."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(
                        success=False, turn_id=None, error="conflict"
                    )
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=11)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=77)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch(
            "src.app.ensure_queue_processor", new=AsyncMock()
        ) as mock_ensure_queue:
            handled = await _route_codex_message_to_active_turn_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.add_to_queue.assert_awaited_once()
        deps.codex_executor.record_queue_fallback.assert_awaited_once_with(success=True)
        mock_ensure_queue.assert_awaited_once()
        deps.db.update_command_status.assert_any_await(
            11,
            "completed",
            output="Steer failed (conflict). Auto-queued item #77.",
        )

    @pytest.mark.asyncio
    async def test_reports_queue_failure_after_steer_failure(self):
        """If queue fallback fails, command status should be marked failed and user notified."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(
                        success=False, turn_id=None, error="busy"
                    )
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=12)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(side_effect=RuntimeError("db insert failed")),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_codex_message_to_active_turn_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.update_command_status.assert_any_await(
            12,
            "failed",
            output="Steer failed and queue fallback failed. steer_error=busy queue_error=db insert failed",
            error_message="db insert failed",
        )
        deps.codex_executor.record_queue_fallback.assert_awaited_once_with(
            success=False
        )
        assert client.chat_postMessage.await_count >= 1

    @pytest.mark.asyncio
    async def test_reports_queue_start_failure_after_steer_failure(self):
        """Queued Codex fallback should report queue startup failures instead of going silent."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(
                        success=False, turn_id=None, error="conflict"
                    )
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=13)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=78)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch(
            "src.app.ensure_queue_processor",
            new=AsyncMock(side_effect=RuntimeError("queue task start failed")),
        ):
            handled = await _route_codex_message_to_active_turn_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.update_command_status.assert_any_await(
            13,
            "failed",
            output=(
                "Steer failed (conflict). Auto-queued item #78, "
                "but queue processor startup failed: queue task start failed"
            ),
            error_message="queue task start failed",
        )
        assert client.chat_postMessage.await_count >= 1


class TestClaudeActiveExecutionRouting:
    """Tests for active Claude execution queue fallback behavior."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_active_execution(self):
        """No active Claude execution should fall through to normal runtime execution."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=False),
                is_live_pty_enabled=MagicMock(return_value=False),
                has_active_live_pty=AsyncMock(return_value=False),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_claude_message_to_active_execution_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is False
        deps.db.add_command.assert_not_awaited()
        deps.db.add_to_queue.assert_not_awaited()
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_queues_message_when_active_execution_exists(self):
        """Active Claude execution should auto-queue follow-up messages."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=True),
                is_live_pty_enabled=MagicMock(return_value=False),
                has_active_live_pty=AsyncMock(return_value=False),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=21)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=88)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch(
            "src.app.ensure_queue_processor", new=AsyncMock()
        ) as mock_ensure_queue:
            handled = await _route_claude_message_to_active_execution_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.add_to_queue.assert_awaited_once()
        mock_ensure_queue.assert_awaited_once()
        deps.db.update_command_status.assert_any_await(
            21,
            "completed",
            output="Active Claude execution detected. Auto-queued item #88.",
        )

    @pytest.mark.asyncio
    async def test_reports_queue_start_failure_when_active_claude_execution_exists(
        self,
    ):
        """Queued Claude fallback should report queue startup failures instead of going silent."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=True),
                is_live_pty_enabled=MagicMock(return_value=False),
                has_active_live_pty=AsyncMock(return_value=False),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=22)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=89)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch(
            "src.app.ensure_queue_processor",
            new=AsyncMock(side_effect=RuntimeError("queue task start failed")),
        ):
            handled = await _route_claude_message_to_active_execution_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.update_command_status.assert_any_await(
            22,
            "failed",
            output=(
                "Active Claude execution detected. Auto-queued item #89, "
                "but queue processor startup failed: queue task start failed"
            ),
            error_message="queue task start failed",
        )
        assert client.chat_postMessage.await_count >= 1

    @pytest.mark.asyncio
    async def test_reports_queue_failure_when_active_execution_exists(self):
        """Queue fallback failures should update command status and notify user."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=True),
                is_live_pty_enabled=MagicMock(return_value=False),
                has_active_live_pty=AsyncMock(return_value=False),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=22)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(side_effect=RuntimeError("db insert failed")),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_claude_message_to_active_execution_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.update_command_status.assert_any_await(
            22,
            "failed",
            output=(
                "Active Claude execution detected but queue fallback failed."
                " queue_error=db insert failed"
            ),
            error_message="db insert failed",
        )
        assert client.chat_postMessage.await_count >= 1

    @pytest.mark.asyncio
    async def test_routes_to_active_live_pty_when_steer_succeeds(self):
        """Active live PTY turn should consume follow-up message via steer."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=True),
                is_live_pty_enabled=MagicMock(return_value=True),
                has_active_live_pty=AsyncMock(return_value=True),
                steer_active_execution=AsyncMock(
                    return_value=SimpleNamespace(
                        success=True, turn_id="pty-turn-1", error=None
                    )
                ),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=31)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_claude_message_to_active_execution_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.add_to_queue.assert_not_awaited()
        deps.db.update_command_status.assert_any_await(
            31,
            "completed",
            output="Routed to active Claude PTY turn via live input. turn_id=pty-turn-1",
        )

    @pytest.mark.asyncio
    async def test_queues_when_active_live_pty_steer_fails(self):
        """Live PTY steer failure should fall back to queue processing."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            executor=SimpleNamespace(
                has_active_execution=AsyncMock(return_value=True),
                is_live_pty_enabled=MagicMock(return_value=True),
                has_active_live_pty=AsyncMock(return_value=True),
                steer_active_execution=AsyncMock(
                    return_value=SimpleNamespace(
                        success=False, turn_id=None, error="busy"
                    )
                ),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=32)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=90)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch(
            "src.app.ensure_queue_processor", new=AsyncMock()
        ) as mock_ensure_queue:
            handled = await _route_claude_message_to_active_execution_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.add_to_queue.assert_awaited_once()
        mock_ensure_queue.assert_awaited_once()
        deps.db.update_command_status.assert_any_await(
            32,
            "completed",
            output="Active Claude execution detected (steer failed: busy). Auto-queued item #90.",
        )


class TestStructuredQueuePlanRouting:
    """Tests for structured queue-plan message routing."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_queue_plan_markers(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _queue_structured_plan_message(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts=None,
            prompt="normal prompt text",
            logger=MagicMock(),
        )

        assert handled is False
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_marker_is_reported_as_structured_plan_error(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _queue_structured_plan_message(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts=None,
            prompt="***loop-0",
            logger=MagicMock(),
        )

        assert handled is True
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "Invalid structured queue plan" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_queues_structured_plan_message_items(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[
                        SimpleNamespace(id=1, position=1),
                        SimpleNamespace(id=2, position=2),
                    ]
                ),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
                update_queue_control_state=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            prompt="first",
                            working_directory_override=None,
                            parallel_group_id=None,
                            parallel_limit=None,
                        ),
                        SimpleNamespace(
                            prompt="second",
                            working_directory_override="/repo-worktrees/feature-x",
                            parallel_group_id=None,
                            parallel_limit=None,
                        ),
                    ]
                ),
            ):
                with patch(
                    "src.app.ensure_queue_processor", new=AsyncMock()
                ) as mock_ensure:
                    handled = await _queue_structured_plan_message(
                        client=client,
                        deps=deps,
                        session=session,
                        channel_id="C123",
                        thread_ts="123.456",
                        prompt="first\n***\nsecond",
                        logger=MagicMock(),
                    )

        assert handled is True
        deps.db.add_many_to_queue.assert_awaited_once_with(
            session_id=1,
            channel_id="C123",
            thread_ts="123.456",
            queue_entries=[
                ("first", None, None, None),
                ("second", "/repo-worktrees/feature-x", None, None),
            ],
            replace_pending=False,
            insertion_mode="append",
            insert_at=None,
        )
        mock_ensure.assert_awaited_once()
        assert (
            "Added 2 item(s) from structured plan."
            in client.chat_postMessage.await_args.kwargs["text"]
        )

    @pytest.mark.asyncio
    async def test_queues_structured_plan_mode_directive_into_item_metadata(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[SimpleNamespace(id=1, position=1)]
                ),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
                update_queue_control_state=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            prompt="first",
                            working_directory_override=None,
                            parallel_group_id=None,
                            parallel_limit=None,
                            mode_directive="plan",
                        )
                    ]
                ),
            ):
                with patch("src.app.ensure_queue_processor", new=AsyncMock()):
                    handled = await _queue_structured_plan_message(
                        client=client,
                        deps=deps,
                        session=session,
                        channel_id="C123",
                        thread_ts="123.456",
                        prompt="(mode: plan)\nfirst\n(end)",
                        logger=MagicMock(),
                    )

        assert handled is True
        deps.db.add_many_to_queue.assert_awaited_once_with(
            session_id=1,
            channel_id="C123",
            thread_ts="123.456",
            queue_entries=[
                (
                    "first",
                    None,
                    None,
                    None,
                    {"runtime_mode_directive": "plan"},
                )
            ],
            replace_pending=False,
            insertion_mode="append",
            insert_at=None,
        )

    @pytest.mark.asyncio
    async def test_appended_structured_plan_message_keeps_pending_queue_when_directed(
        self,
    ):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[SimpleNamespace(id=1, position=3)]
                ),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            prompt="next",
                            working_directory_override=None,
                            parallel_group_id=None,
                            parallel_limit=None,
                        )
                    ]
                ),
            ):
                with patch("src.app.ensure_queue_processor", new=AsyncMock()):
                    handled = await _queue_structured_plan_message(
                        client=client,
                        deps=deps,
                        session=session,
                        channel_id="C123",
                        thread_ts="123.456",
                        prompt="(append)\nnext",
                        logger=MagicMock(),
                    )

        assert handled is True
        deps.db.add_many_to_queue.assert_awaited_once_with(
            session_id=1,
            channel_id="C123",
            thread_ts="123.456",
            queue_entries=[("next", None, None, None)],
            replace_pending=False,
            insertion_mode="append",
            insert_at=None,
        )

    @pytest.mark.asyncio
    async def test_structured_plan_message_rejects_clear_directive(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _queue_structured_plan_message(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="(clear)\nfirst",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.add_many_to_queue.assert_not_awaited()
        assert (
            "handled by `/qc clear`"
            in client.chat_postMessage.await_args.kwargs["text"]
        )

    @pytest.mark.asyncio
    async def test_structured_plan_message_persists_scheduled_controls(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=30)
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[SimpleNamespace(id=1, position=1)]
                ),
                add_queue_scheduled_events=AsyncMock(
                    return_value=[SimpleNamespace(id=900)]
                ),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="running")
                ),
                update_queue_control_state=AsyncMock(
                    return_value=SimpleNamespace(state="paused")
                ),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.parse_queue_plan_submission",
                return_value=(
                    SimpleNamespace(
                        replace_pending=True,
                        insertion_mode="append",
                        insert_at=None,
                        scheduled_controls=[
                            SimpleNamespace(action="pause", execute_at=scheduled_time),
                        ],
                    ),
                    "first",
                ),
            ):
                with patch(
                    "src.app.materialize_queue_plan_text",
                    new=AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                prompt="first",
                                working_directory_override=None,
                                parallel_group_id=None,
                                parallel_limit=None,
                            )
                        ]
                    ),
                ):
                    with patch("src.app.ensure_queue_processor", new=AsyncMock()):
                        with patch(
                            "src.app.ensure_queue_schedule_dispatcher", new=AsyncMock()
                        ) as mock_scheduler:
                            handled = await _queue_structured_plan_message(
                                client=client,
                                deps=deps,
                                session=session,
                                channel_id="C123",
                                thread_ts="123.456",
                                prompt="(at 19:00 pause)\nfirst",
                                logger=MagicMock(),
                            )

        assert handled is True
        deps.db.update_queue_control_state.assert_awaited_once_with(
            "C123", "123.456", "paused"
        )
        deps.db.add_queue_scheduled_events.assert_awaited_once_with(
            channel_id="C123",
            thread_ts="123.456",
            events=[("pause", scheduled_time)],
        )
        mock_scheduler.assert_awaited_once()
        assert (
            "Scheduled controls:" in client.chat_postMessage.await_args.kwargs["text"]
        )

    @pytest.mark.asyncio
    async def test_structured_plan_queue_restarts_when_replacing_paused_queue(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[SimpleNamespace(id=10, position=10)]
                ),
                get_running_queue_items=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(
                    return_value=SimpleNamespace(state="paused")
                ),
                update_queue_control_state=AsyncMock(),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            prompt="do work",
                            working_directory_override=None,
                            parallel_group_id=None,
                            parallel_limit=None,
                        )
                    ]
                ),
            ):
                with patch(
                    "src.app.ensure_queue_processor", new=AsyncMock()
                ) as mock_ensure:
                    handled = await _queue_structured_plan_message(
                        client=client,
                        deps=deps,
                        session=session,
                        channel_id="C123",
                        thread_ts="123.456",
                        prompt="do work",
                        logger=MagicMock(),
                    )

        assert handled is True
        deps.db.update_queue_control_state.assert_not_awaited()
        mock_ensure.assert_not_awaited()
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "Queue is paused" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_reports_invalid_structured_plan(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(side_effect=ValueError("bad marker")),
            ):
                handled = await _queue_structured_plan_message(
                    client=client,
                    deps=deps,
                    session=session,
                    channel_id="C123",
                    thread_ts=None,
                    prompt="***loop-0",
                    logger=MagicMock(),
                )

        assert handled is True
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "Failed to process structured queue plan" in kwargs["text"]


class TestStartupQueueRecovery:
    """Tests for startup queue processor recovery."""

    @pytest.mark.asyncio
    async def test_restore_pending_queue_processors_starts_only_running_scopes(self):
        deps = SimpleNamespace(
            db=SimpleNamespace(
                list_pending_queue_scopes=AsyncMock(
                    return_value=[
                        ("C123", None),
                        ("C123", "111.222"),
                        ("C999", None),
                    ]
                ),
                get_queue_control=AsyncMock(
                    side_effect=[
                        SimpleNamespace(state="running"),
                        SimpleNamespace(state="paused"),
                        SimpleNamespace(state="running"),
                    ]
                ),
            )
        )
        client = SimpleNamespace()
        fake_logger = MagicMock()

        with patch("src.app.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
            await _restore_pending_queue_processors(
                client=client, deps=deps, logger=fake_logger
            )

        assert mock_ensure.await_count == 2
        scopes_started = {
            (
                call.kwargs["channel_id"],
                call.kwargs["thread_ts"],
            )
            for call in mock_ensure.await_args_list
        }
        assert scopes_started == {("C123", None), ("C999", None)}

    @pytest.mark.asyncio
    async def test_restore_pending_queue_processors_noop_without_pending_scopes(self):
        deps = SimpleNamespace(
            db=SimpleNamespace(
                list_pending_queue_scopes=AsyncMock(return_value=[]),
                get_queue_control=AsyncMock(),
            )
        )
        client = SimpleNamespace()
        fake_logger = MagicMock()

        with patch("src.app.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
            await _restore_pending_queue_processors(
                client=client, deps=deps, logger=fake_logger
            )

        mock_ensure.assert_not_awaited()
        deps.db.get_queue_control.assert_not_awaited()
