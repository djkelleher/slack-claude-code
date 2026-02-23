# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Run all tests
pytest

# Run single test file
pytest tests/unit/test_repository.py

# Run single test
pytest tests/unit/test_repository.py::test_function_name -v

# Run with coverage
pytest --cov=src

# Format code
black src/ tests/ --line-length 100

# Sort imports
isort src/ tests/ --profile black

# Lint
flake8 src/ tests/ --max-complexity 10
```

## Architecture

This is a Slack bot that wraps Claude Code CLI, providing a Slack interface for remote access.

**Core Flow:**
1. Slack events/commands arrive via Socket Mode (`app.py`)
2. Commands are routed to handlers in `src/handlers/`
3. Claude CLI is invoked via `SubprocessExecutor` which streams JSON output
4. Responses are formatted and posted back to Slack with `SlackFormatter`

**Key Components:**

- `SubprocessExecutor` (`src/claude/subprocess_executor.py`): Executes Claude CLI with `--output-format stream-json`, handles streaming responses, manages process lifecycle, and detects special events (AskUserQuestion, ExitPlanMode)
- `StreamParser` (`src/claude/streaming.py`): Parses Claude CLI's stream-json output into typed `StreamMessage` objects
- `DatabaseRepository` (`src/database/repository.py`): SQLite persistence for sessions, queue items, jobs, notification settings. Uses WAL mode and UPSERT for concurrent access
- `SlackFormatter` (`src/utils/formatting.py`): Converts Claude output to Slack Block Kit format with syntax highlighting, tables, and collapsible sections

**Session Model:** Each Slack channel is one session. Threads within a channel create separate isolated sessions (tracked by `thread_ts`).

**Handler Pattern:** All command handlers use the `@slack_command()` decorator from `src/handlers/base.py` which handles ack, context creation, validation, and error formatting.

## Code Style

- Formatter: black (100 char lines)
- Import sorting: isort (black profile)
- Linting: flake8 (max complexity 10)
- Type hints required on all function signatures
- Docstrings: NumPy convention

## Naming Conventions

- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private: leading underscore (`_method`)
- Enum values: lowercase string values (`OrderStatus.PENDING = "pending"`)

## Import Rules

- All imports at top of files - no lazy imports, no `try/except ImportError`
- Never use ImportError handling - assume all imports succeed
- No `__all__` definitions
- No backward compatibility re-exports or wrappers
- Order: stdlib → third-party → local

## General Rules

- Do not use `getattr` or `hasattr` - use direct attribute access
- Do not revert files to older git commits without asking first
- Do not create nested folders with only a single file
- Commit all changes after code is modified with detailed commit messages
