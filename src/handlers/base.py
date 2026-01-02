"""Base infrastructure for Slack command handlers."""

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from src.utils.formatting import SlackFormatter

# Maximum length for command input to prevent resource exhaustion
MAX_PROMPT_LENGTH = 50000


@dataclass
class CommandContext:
    """Unified context for command execution.

    Extracts common fields from Slack command dict and provides
    typed access to the Slack client and logger.
    """

    channel_id: str
    user_id: str
    text: str
    command_name: str
    client: Any
    logger: Any

    @classmethod
    def from_command(cls, command: dict, client: Any, logger: Any) -> "CommandContext":
        """Create context from Slack command dict.

        Parameters
        ----------
        command : dict
            The command payload from Slack.
        client : Any
            The Slack WebClient for API calls.
        logger : Any
            Logger instance for this request.

        Returns
        -------
        CommandContext
            Populated context object.
        """
        return cls(
            channel_id=command["channel_id"],
            user_id=command["user_id"],
            text=command.get("text", "").strip(),
            command_name=command.get("command", ""),
            client=client,
            logger=logger,
        )


@dataclass
class HandlerDependencies:
    """Container for handler dependencies.

    Provides access to shared instances across all handlers.
    Optional dependencies are lazily initialized on first access.
    """

    db: Any  # DatabaseRepository
    executor: Any  # ClaudeExecutor
    _orchestrator: Any = field(default=None, repr=False)
    _usage_checker: Any = field(default=None, repr=False)
    _budget_scheduler: Any = field(default=None, repr=False)

    @property
    def orchestrator(self) -> Any:
        """Get or create the MultiAgentOrchestrator."""
        if self._orchestrator is None:
            from src.agents import MultiAgentOrchestrator

            self._orchestrator = MultiAgentOrchestrator(self.executor)
        return self._orchestrator

    @property
    def usage_checker(self) -> Any:
        """Get or create the UsageChecker."""
        if self._usage_checker is None:
            from src.budget import UsageChecker

            self._usage_checker = UsageChecker()
        return self._usage_checker

    @property
    def budget_scheduler(self) -> Any:
        """Get or create the BudgetScheduler."""
        if self._budget_scheduler is None:
            from src.budget import BudgetScheduler

            self._budget_scheduler = BudgetScheduler()
        return self._budget_scheduler


def slack_command(
    require_text: bool = False,
    usage_hint: str = "",
    max_length: int = MAX_PROMPT_LENGTH,
) -> Callable:
    """Decorator for Slack command handlers.

    Handles common boilerplate:
    - Automatic ack() call
    - CommandContext creation
    - Optional text validation
    - Input length validation
    - Exception handling with error message formatting

    Parameters
    ----------
    require_text : bool
        If True, validates that command text is not empty.
    usage_hint : str
        Usage hint shown when text validation fails.
    max_length : int
        Maximum allowed length for input text.

    Returns
    -------
    Callable
        Decorated handler function.

    Examples
    --------
    >>> @app.command("/mycommand")
    ... @slack_command(require_text=True, usage_hint="Usage: /mycommand <arg>")
    ... async def handle_mycommand(ctx: CommandContext, deps: HandlerDependencies):
    ...     await ctx.client.chat_postMessage(
    ...         channel=ctx.channel_id,
    ...         text=f"You said: {ctx.text}",
    ...     )
    """

    def decorator(func: Callable) -> Callable:
        async def wrapper(ack, command, client, logger, **kwargs):
            await ack()

            ctx = CommandContext.from_command(command, client, logger)

            if require_text and not ctx.text:
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(
                        f"Please provide input. {usage_hint}"
                    ),
                )
                return

            # Validate input length to prevent resource exhaustion
            if len(ctx.text) > max_length:
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(
                        f"Input too long ({len(ctx.text):,} chars). "
                        f"Maximum is {max_length:,} characters."
                    ),
                )
                return

            try:
                await func(ctx, **kwargs)
            except Exception as e:
                logger.error(f"Error in {ctx.command_name}: {e}\n{traceback.format_exc()}")
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(str(e)),
                )

        return wrapper

    return decorator
