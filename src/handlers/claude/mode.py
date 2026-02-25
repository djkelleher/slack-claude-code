"""Mode command handler: /mode for Claude and Codex sessions."""

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import (
    SUPPORTED_COMPAT_MODE_ALIASES,
    codex_mode_alias_for_approval,
    resolve_codex_compat_mode,
)
from src.config import config
from src.database.models import Session
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, slack_command

# Mode aliases: short name -> CLI mode value
CLAUDE_MODE_ALIASES = {
    "bypass": config.DEFAULT_BYPASS_MODE,
    "accept": "acceptEdits",
    "default": "default",
    "plan": "plan",
    "ask": "default",
    "delegate": "delegate",
}

# Reverse lookup for display
CLAUDE_MODE_DISPLAY = {v: k for k, v in CLAUDE_MODE_ALIASES.items()}


def register_mode_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register mode command handler.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/mode")
    @slack_command(
        require_text=False, usage_hint="Usage: /mode [bypass|accept|plan|ask|default|delegate]"
    )
    async def handle_mode(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mode command - view or set permission mode for session."""
        text = ctx.text.strip().lower() if ctx.text else ""

        # Get session
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        backend = session.get_backend()

        if backend == "codex":
            await _handle_codex_mode(ctx, deps, session, text)
            return

        # No argument: show current mode
        if not text:
            current_mode = session.permission_mode or config.CLAUDE_PERMISSION_MODE
            display_mode = CLAUDE_MODE_DISPLAY.get(current_mode, current_mode)

            mode_list = "\n".join(
                f"• `{alias}` - {_get_mode_description(alias)}" for alias in CLAUDE_MODE_ALIASES
            )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current mode: {display_mode}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Current permission mode:* `{display_mode}`\n\n*Available modes:*\n{mode_list}",
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/mode <name>` to change the mode for this session.",
                            }
                        ],
                    },
                ],
            )
            return

        # Check if it's a valid mode alias
        if text not in CLAUDE_MODE_ALIASES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Unknown mode: {text}",
                blocks=SlackFormatter.error_message(
                    f"Unknown mode: `{text}`\n\nValid modes: {', '.join(f'`{m}`' for m in CLAUDE_MODE_ALIASES)}"
                ),
            )
            return

        # Set the mode
        cli_mode = CLAUDE_MODE_ALIASES[text]
        await deps.db.update_session_mode(ctx.channel_id, ctx.thread_ts, cli_mode)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Mode set to: {text}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":heavy_check_mark: Permission mode set to `{text}`\n\n{_get_mode_description(text)}",
                    },
                },
            ],
        )


async def _handle_codex_mode(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    text: str,
) -> None:
    """Handle /mode for Codex sessions."""
    if not text:
        current_mode = _get_codex_display_mode(
            permission_mode=session.permission_mode,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
        )
        mode_list = "\n".join(
            f"• `{alias}` - {_get_codex_mode_description(alias)}"
            for alias in SUPPORTED_COMPAT_MODE_ALIASES
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Current mode: {current_mode}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Current Codex mode:* `{current_mode}`\n\n*Available modes:*\n{mode_list}",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Use `/approval` and `/sandbox` for direct Codex controls.",
                        }
                    ],
                },
            ],
        )
        return

    resolved = resolve_codex_compat_mode(text)
    if resolved.error:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Invalid Codex mode: {text}",
            blocks=SlackFormatter.error_message(resolved.error),
        )
        return

    cli_mode = _map_codex_alias_to_permission_mode(text)
    await deps.db.update_session_mode(ctx.channel_id, ctx.thread_ts, cli_mode)
    if resolved.approval_mode:
        await deps.db.update_session_approval_mode(
            ctx.channel_id, ctx.thread_ts, resolved.approval_mode
        )

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Codex mode set to: {text}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: Codex mode set to `{text}`\n\n"
                        f"{_get_codex_mode_description(text)}"
                    ),
                },
            }
        ],
    )


def _map_codex_alias_to_permission_mode(alias: str) -> str:
    """Map Codex `/mode` alias to stored permission mode."""
    if alias == "bypass":
        return config.DEFAULT_BYPASS_MODE
    if alias == "plan":
        return "plan"
    return "default"


def _get_codex_display_mode(permission_mode: str | None, approval_mode: str | None) -> str:
    """Get Codex mode alias for display."""
    if (permission_mode or "").strip().lower() == "plan":
        return "plan"
    return codex_mode_alias_for_approval(approval_mode)


def _get_codex_mode_description(mode: str) -> str:
    """Get human-readable mode description for Codex sessions."""
    descriptions = {
        "bypass": "Set approval mode to `never`.",
        "ask": "Set approval mode to `on-request`.",
        "default": "Alias of `ask` for compatibility.",
        "plan": "Plan-first mode; ask for a concrete plan before execution.",
    }
    return descriptions.get(mode, "")


def _get_mode_description(mode: str) -> str:
    """Get a human-readable description for a mode."""
    descriptions = {
        "bypass": "Auto-approve all operations (files, commands, etc.)",
        "accept": "Auto-accept file edits only",
        "plan": "Plan mode - assistant provides a plan before execution",
        "ask": "Default behavior - Claude asks for permission before operations",
        "default": "Default Claude behavior",
        "delegate": "Delegate permission decisions",
    }
    return descriptions.get(mode, "")
