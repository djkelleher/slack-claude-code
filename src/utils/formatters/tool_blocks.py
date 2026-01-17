"""Tool activity formatting for Slack Block Kit.

Formats tool invocations (Read, Edit, Write, Bash, etc.) for display
in Slack messages during streaming updates.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from .base import escape_markdown, truncate_output

if TYPE_CHECKING:
    from src.claude.streaming import ToolActivity


# Tool icons for Slack display (disabled - emojis removed)
TOOL_ICONS = {
    "Read": "",
    "Edit": "",
    "Write": "",
    "Bash": "",
    "Glob": "",
    "Grep": "",
    "WebFetch": "",
    "WebSearch": "",
    "LSP": "",
    "TodoWrite": "",
    "Task": "",
    "NotebookEdit": "",
    "AskUserQuestion": "",
}


def get_tool_icon(tool_name: str) -> str:
    """Get the icon for a tool, with fallback."""
    return TOOL_ICONS.get(tool_name, "")


def format_tool_inline(tool: "ToolActivity") -> str:
    """Format tool as compact inline text for streaming display.

    Parameters
    ----------
    tool : ToolActivity
        The tool activity to format.

    Returns
    -------
    str
        Formatted string like "*Read* `src/app.py`"
    """
    icon = get_tool_icon(tool.name)
    # Only include icon if it's not empty
    base = f"{icon} *{tool.name}*" if icon else f"*{tool.name}*"

    if tool.input_summary:
        base += f" {tool.input_summary}"

    return base


def format_tool_status(tool: "ToolActivity") -> str:
    """Format tool status indicator (running, success, error).

    Parameters
    ----------
    tool : ToolActivity
        The tool activity to format.

    Returns
    -------
    str
        Status string like "OK (12ms)" or "..."
    """
    if tool.result is None and not tool.is_error:
        # Still running
        return "..."

    if tool.is_error:
        status = "ERROR"
    else:
        status = "OK"

    if tool.duration_ms is not None:
        return f"{status} ({tool.duration_ms}ms)"
    return status


def format_tool_timestamp(tool: "ToolActivity") -> str:
    """Format tool timestamp for display.

    Parameters
    ----------
    tool : ToolActivity
        The tool activity to format.

    Returns
    -------
    str
        Formatted timestamp like "14:32:05"
    """
    if tool.timestamp:
        dt = datetime.fromtimestamp(tool.timestamp)
        return dt.strftime("%H:%M:%S")
    return ""


def format_tool_activity_line(tool: "ToolActivity") -> str:
    """Format a single tool activity as one line for the activity feed.

    Parameters
    ----------
    tool : ToolActivity
        The tool activity to format.

    Returns
    -------
    str
        Full formatted line like "14:32:05 *Read* `src/app.py` OK (12ms)"
    """
    timestamp = format_tool_timestamp(tool)
    inline = format_tool_inline(tool)
    status = format_tool_status(tool)
    if timestamp:
        return f"`{timestamp}` {inline} {status}"
    return f"{inline} {status}"


def format_tool_activity_section(
    tools: list["ToolActivity"],
    max_display: int = 8,
) -> list[dict]:
    """Format tool activities as a Slack block section.

    Parameters
    ----------
    tools : list[ToolActivity]
        List of tool activities to format.
    max_display : int
        Maximum number of tools to display (shows most recent).

    Returns
    -------
    list[dict]
        Slack blocks for the tool activity section.
    """
    if not tools:
        return []

    blocks = []

    # Show most recent tools
    display_tools = tools[-max_display:] if len(tools) > max_display else tools

    # Build activity lines
    lines = [format_tool_activity_line(tool) for tool in display_tools]

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "_*Tool Activity:*_\n" + "\n".join(lines),
            },
        }
    )

    # Add count indicator if truncated
    if len(tools) > max_display:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Showing {max_display} of {len(tools)} tools_",
                    }
                ],
            }
        )

    return blocks


def format_tool_detail_blocks(tool: "ToolActivity") -> list[dict]:
    """Format full tool details for modal or thread reply.

    Parameters
    ----------
    tool : ToolActivity
        The tool activity to format in detail.

    Returns
    -------
    list[dict]
        Slack blocks with full tool input/output details.
    """
    icon = get_tool_icon(tool.name)
    blocks = []

    # Header with status
    status_text = format_tool_status(tool)
    header_text = f"{icon} *{tool.name}* {status_text}" if icon else f"*{tool.name}* {status_text}"
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": header_text,
            },
        }
    )

    # Input section
    input_text = _format_tool_input_detail(tool.name, tool.input)
    if input_text:
        # Truncate input for display
        input_display = truncate_output(input_text, 1500)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Input:*\n```{escape_markdown(input_display)}```",
                },
            }
        )

    # Result section (if available)
    if tool.result is not None or tool.is_error:
        blocks.append({"type": "divider"})

        result_text = tool.full_result if tool.full_result else tool.result
        if result_text:
            # Truncate result for display
            result_display = truncate_output(result_text, 2000)
            result_label = "*Error:*" if tool.is_error else "*Result:*"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{result_label}\n```{escape_markdown(result_display)}```",
                    },
                }
            )

    return blocks


def _format_tool_input_detail(tool_name: str, input_dict: dict) -> str:
    """Format tool input parameters for detailed display.

    Parameters
    ----------
    tool_name : str
        Name of the tool.
    input_dict : dict
        Tool input parameters.

    Returns
    -------
    str
        Formatted input string.
    """
    if not input_dict:
        return ""

    if tool_name == "Read":
        path = input_dict.get("file_path", "?")
        offset = input_dict.get("offset")
        limit = input_dict.get("limit")
        result = f"file_path: {path}"
        if offset:
            result += f"\noffset: {offset}"
        if limit:
            result += f"\nlimit: {limit}"
        return result

    elif tool_name == "Edit":
        path = input_dict.get("file_path", "?")
        old_str = input_dict.get("old_string", "")
        new_str = input_dict.get("new_string", "")
        old_preview = old_str[:200] + "..." if len(old_str) > 200 else old_str
        new_preview = new_str[:200] + "..." if len(new_str) > 200 else new_str
        return f"file_path: {path}\nold_string: {old_preview}\nnew_string: {new_preview}"

    elif tool_name == "Write":
        path = input_dict.get("file_path", "?")
        content = input_dict.get("content", "")
        content_preview = content[:300] + "..." if len(content) > 300 else content
        return f"file_path: {path}\ncontent ({len(content)} chars):\n{content_preview}"

    elif tool_name == "Bash":
        cmd = input_dict.get("command", "?")
        desc = input_dict.get("description", "")
        result = f"command: {cmd}"
        if desc:
            result += f"\ndescription: {desc}"
        return result

    elif tool_name in ("Glob", "Grep"):
        lines = []
        for key in ["pattern", "path", "glob", "type", "output_mode"]:
            if key in input_dict:
                lines.append(f"{key}: {input_dict[key]}")
        return "\n".join(lines)

    elif tool_name == "Task":
        desc = input_dict.get("description", "")
        prompt = input_dict.get("prompt", "")
        subagent = input_dict.get("subagent_type", "")
        result = f"description: {desc}" if desc else ""
        if prompt:
            prompt_preview = prompt[:300] + "..." if len(prompt) > 300 else prompt
            result += f"\nprompt: {prompt_preview}"
        if subagent:
            result += f"\nsubagent_type: {subagent}"
        return result.strip()

    elif tool_name == "AskUserQuestion":
        questions = input_dict.get("questions", [])
        lines = []
        for i, q in enumerate(questions):
            header = q.get("header", f"Question {i+1}")
            question = q.get("question", "?")
            options = q.get("options", [])
            option_labels = [o.get("label", "?") for o in options]
            lines.append(f"{header}: {question}")
            lines.append(f"  Options: {', '.join(option_labels)}")
        return "\n".join(lines)

    else:
        # Generic formatting
        lines = []
        for key, value in input_dict.items():
            if isinstance(value, str) and len(value) > 100:
                value = value[:100] + "..."
            elif isinstance(value, (list, dict)):
                value = str(value)[:100] + "..." if len(str(value)) > 100 else str(value)
            lines.append(f"{key}: {value}")
        return "\n".join(lines)
