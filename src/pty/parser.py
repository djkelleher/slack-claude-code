"""Terminal output parser for Claude Code PTY sessions.

Handles ANSI escape code stripping, prompt detection, and tool use parsing.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OutputType(Enum):
    """Type of parsed output."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    PROMPT = "prompt"
    PERMISSION = "permission"
    ERROR = "error"


@dataclass
class ParsedChunk:
    """A parsed chunk of terminal output."""

    output_type: OutputType
    text: str
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    is_prompt: bool = False
    is_permission_request: bool = False
    raw: str = ""


@dataclass
class ParsedOutput:
    """Result of parsing terminal output."""

    chunks: list[ParsedChunk] = field(default_factory=list)
    clean_text: str = ""
    has_prompt: bool = False
    has_permission_request: bool = False
    raw_text: str = ""


class TerminalOutputParser:
    """Parser for Claude Code terminal output.

    Strips ANSI codes, detects prompts, permission requests, and tool usage.
    """

    # ANSI escape sequence patterns
    ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    # OSC (Operating System Command) sequences like title changes
    OSC_ESCAPE = re.compile(r"\x1B\][^\x07]*\x07")

    # Control characters to strip (except newline and tab)
    CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

    # Carriage return for progress updates (line overwrites)
    CR_OVERWRITE = re.compile(r"[^\n]*\r(?!\n)")

    # Claude prompt patterns - match the input prompt line
    # Claude Code shows: > Try "create a util..." or just >
    PROMPT_PATTERNS = [
        re.compile(r"^>\s*$", re.MULTILINE),  # Just >
        re.compile(r"^> Try ", re.MULTILINE),  # > Try "suggestion..."
        re.compile(r"^> [A-Z]", re.MULTILINE),  # > followed by capital letter (user input area)
        re.compile(r"^\?\s*$", re.MULTILINE),
        re.compile(r"^claude\s*>\s*$", re.MULTILINE),
        re.compile(r"^\[.*?\]\s*>\s*$", re.MULTILINE),
    ]

    # Permission request patterns
    PERMISSION_PATTERNS = [
        re.compile(r"Claude wants to (use|run|execute|call)", re.IGNORECASE),
        re.compile(r"Allow\s+\w+.*?\[Y/n\]", re.IGNORECASE),
        re.compile(r"Permission required", re.IGNORECASE),
        re.compile(r"needs? your approval", re.IGNORECASE),
        re.compile(r"Do you want to allow", re.IGNORECASE),
    ]

    # Tool use detection patterns
    TOOL_PATTERNS = [
        (re.compile(r"^Running:\s*(.+)$", re.MULTILINE), "bash"),
        (re.compile(r"^Reading:\s*(.+)$", re.MULTILINE), "read"),
        (re.compile(r"^Writing:\s*(.+)$", re.MULTILINE), "write"),
        (re.compile(r"^Editing:\s*(.+)$", re.MULTILINE), "edit"),
        (re.compile(r"^Searching:\s*(.+)$", re.MULTILINE), "grep"),
        (re.compile(r"^Globbing:\s*(.+)$", re.MULTILINE), "glob"),
    ]

    # Claude Code UI noise patterns to remove
    UI_NOISE_PATTERNS = [
        # Horizontal line separators (box drawing chars) - be more inclusive
        re.compile(r"^[\s─━═\-─]{10,}$", re.MULTILINE),
        # Status bar hints - generalize to match any "Try ..." line
        re.compile(r"^\s*Try \".*\".*$", re.MULTILINE),
        re.compile(r"^\s*\? for shortcuts.*$", re.MULTILINE),
        re.compile(r"^\s*◯ /\w+.*$", re.MULTILINE),
        # Output truncation notice
        re.compile(r"^\s*\.+\s*\(earlier output truncated\).*$", re.MULTILINE),
        # Failed marketplace install and other status messages
        re.compile(r"^\s*Failed to install.*$", re.MULTILINE),
        re.compile(r"^\s*Will retry on next startup.*$", re.MULTILINE),
        # ASCII art logo lines - match lines with block chars
        re.compile(r"^[*\s▐▛▜▌▝▘█▀▄░▒▓]+$", re.MULTILINE),
        re.compile(r"^\s*\*[^*]+\*\s*$", re.MULTILINE),
        # Version/model info line from startup
        re.compile(r"^\s*Claude Code v[\d.]+.*$", re.MULTILINE),
        re.compile(r"^\s*(Opus|Sonnet|Haiku).*Claude.*$", re.MULTILINE),
        # Working directory indicator from startup (just tilde paths on their own line)
        re.compile(r"^\s*~/?[\w\-/]*\s*$", re.MULTILINE),
        # Empty box drawing lines
        re.compile(r"^\s*[│┃|]\s*[│┃|]?\s*$", re.MULTILINE),
        # Spinner/progress indicators
        re.compile(r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◑◒◓]+\s*$", re.MULTILINE),
    ]

    def __init__(self) -> None:
        self.buffer = ""
        self._last_was_prompt = False

    def strip_ansi(self, text: str) -> str:
        """Remove all ANSI escape sequences from text."""
        text = self.OSC_ESCAPE.sub("", text)
        text = self.ANSI_ESCAPE.sub("", text)
        text = self.CONTROL_CHARS.sub("", text)
        text = self.CR_OVERWRITE.sub("", text)
        return text

    def clean_ui_noise(self, text: str) -> str:
        """Remove Claude Code UI elements (borders, hints, logo, etc.)."""
        for pattern in self.UI_NOISE_PATTERNS:
            text = pattern.sub("", text)
        # Collapse multiple blank lines into single
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Strip leading/trailing whitespace from each line while preserving structure
        lines = text.split("\n")
        lines = [line.rstrip() for line in lines]
        text = "\n".join(lines)
        return text.strip()

    def clean_for_slack(self, text: str) -> str:
        """Full cleaning pipeline for Slack output."""
        text = self.strip_ansi(text)
        text = self.clean_ui_noise(text)
        return text

    def detect_prompt(self, text: str) -> bool:
        """Check if text contains a Claude prompt."""
        clean = self.strip_ansi(text)
        for pattern in self.PROMPT_PATTERNS:
            if pattern.search(clean):
                return True
        return False

    def detect_permission_request(self, text: str) -> bool:
        """Check if text contains a permission request."""
        clean = self.strip_ansi(text)
        for pattern in self.PERMISSION_PATTERNS:
            if pattern.search(clean):
                return True
        return False

    def detect_tool_use(self, text: str) -> Optional[tuple[str, str]]:
        """Detect tool use from text. Returns (tool_name, input) or None."""
        clean = self.strip_ansi(text)
        for pattern, tool_name in self.TOOL_PATTERNS:
            match = pattern.search(clean)
            if match:
                return (tool_name, match.group(1))
        return None

    def parse(self, raw_data: str) -> ParsedOutput:
        """Parse raw terminal output into structured chunks."""
        self.buffer += raw_data
        result = ParsedOutput(raw_text=raw_data)

        # Strip ANSI codes
        clean = self.strip_ansi(self.buffer)
        result.clean_text = clean

        # Check for prompts
        has_prompt = self.detect_prompt(self.buffer)
        result.has_prompt = has_prompt

        # Check for permission requests
        has_permission = self.detect_permission_request(self.buffer)
        result.has_permission_request = has_permission

        # Split into lines for chunk analysis
        lines = clean.split("\n")

        for i, line in enumerate(lines):
            if not line.strip():
                continue

            chunk = ParsedChunk(
                output_type=OutputType.TEXT,
                text=line + ("\n" if i < len(lines) - 1 else ""),
                raw=line,
            )

            # Check for prompt
            for pattern in self.PROMPT_PATTERNS:
                if pattern.search(line):
                    chunk.output_type = OutputType.PROMPT
                    chunk.is_prompt = True
                    break

            # Check for permission request
            if not chunk.is_prompt:
                for pattern in self.PERMISSION_PATTERNS:
                    if pattern.search(line):
                        chunk.output_type = OutputType.PERMISSION
                        chunk.is_permission_request = True
                        break

            # Check for tool use
            if chunk.output_type == OutputType.TEXT:
                tool_info = self.detect_tool_use(line)
                if tool_info:
                    chunk.output_type = OutputType.TOOL_USE
                    chunk.tool_name, chunk.tool_input = tool_info

            result.chunks.append(chunk)

        # Clear buffer if we found a prompt (response complete)
        if has_prompt:
            self.buffer = ""
            self._last_was_prompt = True
        else:
            self._last_was_prompt = False

        return result

    def parse_incremental(self, raw_data: str) -> ParsedOutput:
        """Parse incremental output without accumulating in buffer.

        Use this for real-time streaming where you don't want to buffer.
        """
        result = ParsedOutput(raw_text=raw_data)
        clean = self.strip_ansi(raw_data)
        result.clean_text = clean
        result.has_prompt = self.detect_prompt(raw_data)
        result.has_permission_request = self.detect_permission_request(raw_data)

        if clean.strip():
            chunk = ParsedChunk(
                output_type=OutputType.TEXT,
                text=clean,
                raw=raw_data,
                is_prompt=result.has_prompt,
                is_permission_request=result.has_permission_request,
            )

            # Check for tool use
            tool_info = self.detect_tool_use(raw_data)
            if tool_info:
                chunk.output_type = OutputType.TOOL_USE
                chunk.tool_name, chunk.tool_input = tool_info

            result.chunks.append(chunk)

        return result

    def reset(self) -> None:
        """Reset parser state."""
        self.buffer = ""
        self._last_was_prompt = False
