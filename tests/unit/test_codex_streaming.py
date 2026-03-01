"""Unit tests for Codex stream parser compatibility."""

from src.codex.streaming import StreamParser, ToolActivity


def test_codex_tool_input_summary_formats_command_and_question():
    """Tool summary should format both command and request_user_input payloads."""
    command_summary = ToolActivity.create_input_summary(
        "run_command", {"command": "echo hello from codex"}
    )
    question_summary = ToolActivity.create_input_summary(
        "request_user_input",
        {"questions": [{"question": "Proceed with deploy?"}]},
    )

    assert "echo hello from codex" in command_summary
    assert "Proceed with deploy?" in question_summary


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


def test_parse_synthetic_assistant_event():
    """Parser should accept executor-generated assistant delta events."""
    parser = StreamParser()
    msg = parser.parse_line('{"type":"assistant","content":"delta text"}')
    assert msg is not None
    assert msg.type == "assistant"
    assert msg.content == "delta text"


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


def test_parse_web_search_item_lifecycle():
    """Parser should map webSearch items to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"ws_1","type":"webSearch","query":"latest release"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "web_search"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"ws_1","type":"webSearch","query":"latest release","action":{"type":"search"}}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert tool_result.tool_activities[0].is_error is False


def test_parse_web_search_item_with_query_in_action():
    """Parser should extract query from action payload when top-level query is absent."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"ws_2","type":"webSearch","action":{"type":"search","query":"pricing error types"}}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "web_search"
    assert "pricing error types" in tool_call.tool_activities[0].input_summary

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"ws_2","type":"webSearch","action":{"type":"search","query":"pricing error types"}}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert "pricing error types" in (tool_result.tool_activities[0].result or "")


def test_parse_fuzzy_file_search_item_lifecycle():
    """Parser should map fuzzyFileSearch items to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"ffs_1","type":"fuzzyFileSearch","query":"executor"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "fuzzy_file_search"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"ffs_1","type":"fuzzyFileSearch","results":[{"path":"src/codex/subprocess_executor.py"}]}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert "returned 1 result" in (tool_result.tool_activities[0].result or "")


def test_parse_file_change_item_lifecycle():
    """Parser should map fileChange items to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"fc_1","type":"fileChange","changes":[{"path":"src/app.py"}]}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "file_change"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"fc_1","type":"fileChange","status":"completed","changes":[{"path":"src/app.py"}]}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert tool_result.tool_activities[0].is_error is False


def test_parse_mcp_tool_call_item_lifecycle():
    """Parser should map mcpToolCall items to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"mcp_1","type":"mcpToolCall","server":"git","tool":"status"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "mcp_tool_call"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"mcp_1","type":"mcpToolCall","status":"completed","server":"git","tool":"status"}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert tool_result.tool_activities[0].is_error is False


def test_parse_reasoning_item_lifecycle():
    """Parser should map reasoning items to tool call/result."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"r_1","type":"reasoning"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "reasoning"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"r_1","type":"reasoning","summary":["step 1","step 2"]}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert "step 1" in (tool_result.tool_activities[0].result or "")


def test_parse_mixed_case_command_execution_and_agent_message():
    """Parser should support mixed-case app-server item type names."""
    parser = StreamParser()
    parser.parse_line('{"type":"thread.started","thread_id":"thread-123"}')

    tool_call = parser.parse_line(
        '{"type":"item.started","item":{"id":"item_9","type":"commandExecution","command":"echo ok"}}'
    )
    assert tool_call is not None
    assert tool_call.type == "tool_call"
    assert tool_call.tool_activities[0].name == "run_command"

    tool_result = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_9","type":"commandExecution","aggregatedOutput":"ok","exitCode":0,"status":"completed"}}'
    )
    assert tool_result is not None
    assert tool_result.type == "tool_result"
    assert tool_result.tool_activities[0].is_error is False

    assistant = parser.parse_line(
        '{"type":"item.completed","item":{"id":"item_10","type":"agentMessage","text":"done"}}'
    )
    assert assistant is not None
    assert assistant.type == "assistant"
    assert assistant.content == "done"
