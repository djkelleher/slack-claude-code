"""Shared semantic tool-summary registry for backend stream parsers."""

SEMANTIC_TOOL_SUMMARY_RULES: dict[str, dict[str, object]] = {
    "read": {"type": "path", "keys": ["path", "file_path"]},
    "edit": {"type": "path", "keys": ["path", "file_path"]},
    "write": {"type": "path", "keys": ["path", "file_path"]},
    "shell": {"type": "cmd", "keys": ["command", "cmd"]},
    "glob": {"type": "pattern", "keys": ["pattern"]},
    "grep": {"type": "pattern", "keys": ["pattern", "query"]},
    "task": {"type": "text", "keys": ["description", "prompt"]},
    "web_fetch": {"type": "url", "keys": ["url"]},
    "web_search": {"type": "text", "keys": ["query"]},
    "lsp": {"type": "lsp", "op_key": "operation", "path_keys": ["filePath"]},
    "todo_write": {"type": "count", "keys": ["todos"], "suffix": " items"},
    "ask_user": {"type": "first_question", "keys": ["questions"]},
    "file_change": {"type": "path", "keys": ["path"]},
    "mcp_tool_call": {"type": "text", "keys": ["server", "tool"]},
    "reasoning": {"type": "text", "keys": ["summary", "content"]},
}


def build_tool_summary_rules(name_map: dict[str, str]) -> dict[str, dict[str, object]]:
    """Build backend-local tool summary rules from semantic tool names."""
    rules: dict[str, dict[str, object]] = {}
    for backend_name, semantic_name in name_map.items():
        rules[backend_name] = dict(SEMANTIC_TOOL_SUMMARY_RULES[semantic_name])
    return rules
