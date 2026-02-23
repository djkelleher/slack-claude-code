"""Mode command handler: /mode (Claude permission modes)."""

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import (
    COMPAT_MODE_ALIASES,
    DEPRECATED_APPROVAL_MODES,
    codex_mode_alias_for_approval,
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)
from src.config import config
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, slack_command

# Mode aliases: short name -> CLI mode value
MODE_ALIASES = {
    "bypass": config.DEFAULT_BYPASS_MODE,
    "accept": "acceptEdits",
    "default": "default",
    "plan": "plan",
    "ask": "default",
    "delegate": "delegate",
}

# Reverse lookup for display
MODE_DISPLAY = {v: k for k, v in MODE_ALIASES.items()}


async def _handle_codex_mode(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session,
    text: str,
) -> None:
    """Handle `/mode` compatibility aliases for Codex sessions."""
    if not text:
        raw_approval = session.approval_mode or config.CODEX_APPROVAL_MODE
        normalized_approval = normalize_codex_approval_mode(raw_approval)
        current_alias = codex_mode_alias_for_approval(raw_approval)
        sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE

        compat_descriptions = {
            "bypass": "Maps to Codex approval mode `never`.",
            "ask": "Maps to Codex approval mode `on-request`.",
            "default": "Alias of `ask` for Codex sessions.",
            "plan": "Not supported in Codex Slack mode.",
            "accept": "Not supported in Codex Slack mode.",
            "delegate": "Not supported in Codex Slack mode.",
        }
        mode_list = "\n".join(
            f"• `{alias}` - {compat_descriptions[alias]}" for alias in COMPAT_MODE_ALIASES
        )

        deprecated_note = ""
        if raw_approval and raw_approval.lower() in DEPRECATED_APPROVAL_MODES:
            deprecated_note = (
                "\n\n:warning: Stored approval mode "
                f"`{raw_approval}` is deprecated; using `{normalized_approval}`."
            )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Current mode: {current_alias}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Current compatibility mode:* "
                            f"`{current_alias}`\n"
                            f"*Codex approval:* `{normalized_approval}`\n"
                            f"*Codex sandbox:* `{sandbox_mode}`"
                            f"{deprecated_note}\n\n"
                            "*Compatibility aliases:*\n"
                            f"{mode_list}"
                        ),
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Use `/approval` and `/sandbox` for native Codex controls.",
                        }
                    ],
                },
            ],
        )
        return

    if text not in COMPAT_MODE_ALIASES:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Unknown mode: {text}",
            blocks=SlackFormatter.error_message(
                f"Unknown mode: `{text}`\n\n"
                f"Valid compatibility modes: {', '.join(f'`{m}`' for m in COMPAT_MODE_ALIASES)}"
            ),
        )
        return

    resolution = resolve_codex_compat_mode(text)
    if resolution.error:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text="Mode is not available for Codex",
            blocks=SlackFormatter.error_message(
                f"{resolution.error}\n\n"
                "Use `/approval` and `/sandbox` for native Codex controls."
            ),
        )
        return

    await deps.db.update_session_approval_mode(
        ctx.channel_id, ctx.thread_ts, resolution.approval_mode
    )
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Mode set to: {text}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: Compatibility mode set to `{text}`\n\n"
                        f"Codex approval is now `{resolution.approval_mode}`."
                    ),
                },
            }
        ],
    )


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
        if session.get_backend() == "codex":
            await _handle_codex_mode(ctx, deps, session, text)
            return

        # No argument: show current mode
        if not text:
            current_mode = session.permission_mode or config.CLAUDE_PERMISSION_MODE
            display_mode = MODE_DISPLAY.get(current_mode, current_mode)

            mode_list = "\n".join(
                f"• `{alias}` - {_get_mode_description(alias)}" for alias in MODE_ALIASES
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
        if text not in MODE_ALIASES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Unknown mode: {text}",
                blocks=SlackFormatter.error_message(
                    f"Unknown mode: `{text}`\n\nValid modes: {', '.join(f'`{m}`' for m in MODE_ALIASES)}"
                ),
            )
            return

        # Set the mode
        cli_mode = MODE_ALIASES[text]
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


def _get_mode_description(mode: str) -> str:
    """Get a human-readable description for a mode."""
    descriptions = {
        "bypass": "Auto-approve all operations (files, commands, etc.)",
        "accept": "Auto-accept file edits only",
        "plan": "Plan mode - Claude plans before executing",
        "ask": "Default behavior - Claude asks for permission before operations",
        "default": "Default Claude behavior",
        "delegate": "Delegate permission decisions",
    }
    return descriptions.get(mode, "")
