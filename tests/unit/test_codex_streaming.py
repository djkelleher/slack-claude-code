"""Unit tests for Codex stream parser compatibility."""

from src.codex.streaming import StreamParser


def test_parse_new_schema_agent_message_and_turn_complete():
    """Parser should extract assistant text from item.completed events."""
    parser = StreamParser()

    init_msg = parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')
    assert init_msg is not None
    assert init_msg.type == "init"
    assert init_msg.session_id == "thread-123"

    assistant_msg = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"hi"}}'
    )
    assert assistant_msg is not None
    assert assistant_msg.type == "assistant"
    assert assistant_msg.content == "hi"

    result_msg = parser.parse_line(
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":1}}'
    )
    assert result_msg is not None
    assert result_msg.type == "result"
    assert result_msg.is_final is True
    assert result_msg.session_id == "thread-123"
    assert result_msg.content == "hi"


def test_parse_new_schema_command_execution_tool_lifecycle():
    """Parser should map command_execution item events to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"/bin/bash -lc \\"ls -1\\"","status":"in_progress"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert len(tool_call.tool_activities) == 1
    assert tool_call.tool_activities[0].id == "item_2"
    assert tool_call.tool_activities[0].name == "run_command"
    assert "ls -1" in tool_call.tool_activities[0].input_summary

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_2","type":"command_execution","aggregated_output":"README.md\\nsrc\\n","exit_code":0,"status":"completed"}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert len(tool_result.tool_activities) == 1
    assert tool_result.tool_activities[0].id == "item_2"
    assert tool_result.tool_activities[0].is_error is False
    assert "README.md" in (tool_result.tool_activities[0].result or "")
    assert "item_2" not in parser.pending_tools


def test_parse_tool_call_with_non_dict_json_input():
    """Parser should normalize non-dict tool input payloads."""
    parser = StreamParser()
    msg = parser.parse_line(
        '{"type":"tool_call","id":"tool_1","name":"run_command","input":"[\\"echo hi\\"]"}'
    )
    assert msg is not None
    assert msg.type == "tool_call"
    assert len(msg.tool_activities) == 1
    assert msg.tool_activities[0].input == {"raw": ["echo hi"]}


def test_parse_command_execution_failed_without_exit_code():
    """Parser should treat explicit failed status as an error without exit_code."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')
    parser.parse_line(
        '{"type":"item.started","item":{"id":"item_3","type":"command_execution","command":"badcmd","status":"in_progress"}}'
    )

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_3","type":"command_execution","status":"failed","error":{"message":"command not found"}}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert len(tool_result.tool_activities) == 1
    assert tool_result.tool_activities[0].id == "item_3"
    assert tool_result.tool_activities[0].is_error is True
    assert "command not found" in (tool_result.tool_activities[0].result or "")


def test_parse_turn_failed_as_error():
    """Parser should surface turn.failed as a final error message."""
    parser = StreamParser()
    error_msg = parser.parse_line('{"type":"turn.failed","error":{"message":"Boom"}}')
    assert error_msg is not None
    assert error_msg.type == "error"
    assert error_msg.content == "Boom"
    assert error_msg.is_final is True


def test_parse_request_user_input_event_as_tool_call():
    """Parser should map request_user_input events to tool calls."""
    parser = StreamParser()
    msg = parser.parse_line(
        '{"type":"request_user_input","call_id":"call_123","questions":[{"question":"Proceed?","header":"Confirm","options":[{"label":"Yes","description":"Continue"}]}]}'
    )
    assert msg is not None
    assert msg.type == "tool_call"
    assert len(msg.tool_activities) == 1
    tool = msg.tool_activities[0]
    assert tool.id == "call_123"
    assert tool.name == "request_user_input"
    assert tool.input["questions"][0]["question"] == "Proceed?"
