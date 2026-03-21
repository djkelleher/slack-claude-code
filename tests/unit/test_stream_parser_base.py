"""Unit tests for shared base stream parser helpers."""

from src.backends.stream_parser_base import BaseStreamParser
from src.utils.stream_models import BaseToolActivity, StreamMessage


class _ToolActivity(BaseToolActivity):
    SUMMARY_RULES = {}


class _Parser(BaseStreamParser):
    def parse_line(self, line: str):
        if line == "skip":
            return None
        return StreamMessage(type="assistant", content=line, raw={})


def test_base_stream_parser_appends_assistant_content_and_converts_non_dict_payload() -> None:
    """Assistant helpers should accumulate plain-text payloads."""
    parser = BaseStreamParser()

    parser._append_assistant_content("")
    parser._append_assistant_content("hello")
    msg = parser._assistant_message_from_non_dict(["world"])

    assert parser.accumulated_content == "hello['world']"
    assert msg.type == "assistant"
    assert msg.content == "['world']"


def test_base_stream_parser_parse_json_line_and_emit_overflow_error() -> None:
    """Overflow from buffered JSON parsing should become a stream error message."""
    parser = BaseStreamParser()

    data, error_msg = parser._parse_json_line('{"ok": true}', max_buffer_size=16)

    assert data == {"ok": True}
    assert error_msg is None

    data, error_msg = parser._parse_json_line("x" * 40, max_buffer_size=16)

    assert data is None
    assert error_msg is not None
    assert error_msg.type == "error"
    assert "overflow" in error_msg.content.lower()


def test_base_stream_parser_tool_helpers_parse_stream_and_reset(monkeypatch) -> None:
    """Base parser should expose tool helper passthroughs, stream iteration, and reset."""
    parser = _Parser()
    activity, _, collision = parser._create_tool_call_activity(
        tool_cls=_ToolActivity,
        tool_id="tool-1",
        tool_name="noop",
        tool_input={},
    )
    parser.pending_tools["tool-1"] = activity
    monkeypatch.setattr("src.backends.stream_parsing_common.time.monotonic", lambda: 5.1)
    activity.started_at = 5.0
    results, _ = parser._create_tool_result_activities(
        tool_cls=_ToolActivity,
        tool_use_id="tool-1",
        content="done",
        is_error=False,
    )

    messages = list(parser.parse_stream(iter(["one", "skip", "two"])))
    parser.buffer = "stale"
    parser.session_id = "session-1"
    parser.accumulated_content = "abc"
    parser.accumulated_detailed = "xyz"
    parser.pending_tools["tool-2"] = activity
    parser.reset()

    assert collision is False
    assert results[0].result == "done"
    assert [msg.content for msg in messages] == ["one", "two"]
    assert parser.buffer == ""
    assert parser.session_id is None
    assert parser.accumulated_content == ""
    assert parser.accumulated_detailed == ""
    assert parser.pending_tools == {}
