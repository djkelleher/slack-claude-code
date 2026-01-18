"""Handler registration for Slack commands and actions."""

from slack_bolt.async_app import AsyncApp

from src.claude.subprocess_executor import SubprocessExecutor
from src.database.repository import DatabaseRepository

from .agents import register_agent_commands
from .base import HandlerDependencies
from .basic import register_basic_commands
from .claude_cli import register_claude_cli_commands
from .git import register_git_commands
from .mode import register_mode_command
from .notifications import register_notifications_command
from .parallel import register_parallel_commands
from .queue import register_queue_commands
from .session_management import register_session_commands


def register_commands(
    app: AsyncApp,
    db: DatabaseRepository,
    executor: SubprocessExecutor,
) -> HandlerDependencies:
    """Register all slash command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    db : DatabaseRepository
        Database repository instance.
    executor : ClaudeExecutor
        Claude executor instance.

    Returns
    -------
    HandlerDependencies
        Container with shared dependencies for access by action handlers.
    """
    deps = HandlerDependencies(db=db, executor=executor)

    register_basic_commands(app, deps)
    register_parallel_commands(app, deps)
    register_queue_commands(app, deps)
    register_claude_cli_commands(app, deps)
    register_agent_commands(app, deps)
    register_mode_command(app, deps)
    register_notifications_command(app, deps)
    register_session_commands(app, deps)
    register_git_commands(app, deps)

    return deps
