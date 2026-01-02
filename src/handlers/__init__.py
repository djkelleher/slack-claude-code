"""Handler registration for Slack commands and actions."""

from slack_bolt.async_app import AsyncApp

from src.claude import ClaudeExecutor
from src.database import DatabaseRepository

from .actions import register_actions
from .agents import register_agent_commands
from .base import CommandContext, HandlerDependencies
from .basic import register_basic_commands
from .budget import register_budget_commands
from .parallel import register_parallel_commands
from .pty import register_pty_commands


def register_commands(
    app: AsyncApp,
    db: DatabaseRepository,
    executor: ClaudeExecutor,
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
    register_agent_commands(app, deps)
    register_budget_commands(app, deps)
    register_pty_commands(app, deps)

    return deps
