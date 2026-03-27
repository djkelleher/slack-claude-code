"""Handler registration for Slack commands and actions."""

from slack_bolt.async_app import AsyncApp

from src.backends.registry import BackendRegistry
from src.claude.sdk_executor import SDKExecutor as ClaudeExecutor
from src.codex.subprocess_executor import SubprocessExecutor as CodexExecutor
from src.database.repository import DatabaseRepository
from src.trace.service import TraceService

from .base import HandlerDependencies
from .basic import register_basic_commands

# Claude-specific handlers
from .claude import (
    register_agents_command,
    register_cancel_commands,
    register_claude_cli_commands,
    register_mode_command,
    register_parallel_commands,
    register_queue_commands,
    register_worktree_commands,
)
from .notifications import register_notifications_command
from .slash_command_router import build_slash_command_router


def register_commands(
    app: AsyncApp,
    db: DatabaseRepository,
    executor: ClaudeExecutor,
    codex_executor: CodexExecutor = None,
    backend_registry: BackendRegistry = None,
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
    codex_executor : CodexExecutor, optional
        Codex subprocess executor instance.
    backend_registry : BackendRegistry, optional
        Central backend and model registry.

    Returns
    -------
    HandlerDependencies
        Container with shared dependencies for access by action handlers.
    """
    deps = HandlerDependencies(
        db=db,
        executor=executor,
        codex_executor=codex_executor,
        backend_registry=backend_registry,
        trace_service=TraceService(db),
    )

    # Shared handlers (work with any backend)
    register_basic_commands(app, deps)
    register_notifications_command(app, deps)

    # Claude-specific handlers
    register_parallel_commands(app, deps)
    register_queue_commands(app, deps)
    register_claude_cli_commands(app, deps)
    register_agents_command(app, deps)
    register_mode_command(app, deps)
    register_worktree_commands(app, deps)
    register_cancel_commands(app, deps)
    deps.slash_command_router = build_slash_command_router(app)

    return deps
