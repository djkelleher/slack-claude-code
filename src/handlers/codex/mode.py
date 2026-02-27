"""Codex mode switching command handlers: /sandbox, /approval."""

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import DEPRECATED_APPROVAL_MODES, normalize_codex_approval_mode
from src.config import config
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command


def register_codex_mode_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Codex mode switching commands."""

    @app.command("/sandbox")
    @slack_command()
    async def handle_sandbox(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Set sandbox mode for the session (Codex)."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        if not ctx.text:
            # Show current mode and available options
            current_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
            modes_list = "\n".join([f"• `{m}`" for m in config.VALID_SANDBOX_MODES])

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":shield: *Current sandbox mode:* `{current_mode}`\n\n*Available modes:*\n{modes_list}",
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/sandbox <mode>` to change the sandbox mode.",
                            }
                        ],
                    },
                ],
            )
            return

        new_mode = ctx.text.lower().strip()

        if new_mode not in config.VALID_SANDBOX_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=error_message(
                    f"Invalid sandbox mode: `{new_mode}`\n\n"
                    f"Valid modes: {', '.join(config.VALID_SANDBOX_MODES)}"
                ),
            )
            return

        await deps.db.update_session_sandbox_mode(ctx.channel_id, ctx.thread_ts, new_mode)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":shield: Sandbox mode updated to `{new_mode}`",
                    },
                }
            ],
        )

    @app.command("/approval")
    @slack_command()
    async def handle_approval(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Set approval mode for the session (Codex)."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        if not ctx.text:
            # Show current mode and available options
            current_mode_raw = session.approval_mode or config.CODEX_APPROVAL_MODE
            current_mode = normalize_codex_approval_mode(current_mode_raw)
            modes_list = "\n".join([f"• `{m}`" for m in config.VALID_APPROVAL_MODES])
            deprecated_note = ""
            if current_mode_raw.lower() in DEPRECATED_APPROVAL_MODES:
                deprecated_note = (
                    f"\n\n:warning: Stored mode `{current_mode_raw}` is deprecated; "
                    f"using `{current_mode}`."
                )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":clipboard: *Current approval mode:* `{current_mode}`\n\n"
                                f"*Available modes:*\n{modes_list}"
                                f"{deprecated_note}"
                            ),
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/approval <mode>` to change the approval mode.",
                            }
                        ],
                    },
                ],
            )
            return

        new_mode = ctx.text.lower().strip()
        if new_mode in DEPRECATED_APPROVAL_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=error_message(
                    f"Approval mode `{new_mode}` is deprecated.\n\n" "Use `on-request` or `never`."
                ),
            )
            return

        if new_mode not in config.VALID_APPROVAL_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=error_message(
                    f"Invalid approval mode: `{new_mode}`\n\n"
                    f"Valid modes: {', '.join(config.VALID_APPROVAL_MODES)}"
                ),
            )
            return

        await deps.db.update_session_approval_mode(ctx.channel_id, ctx.thread_ts, new_mode)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":clipboard: Approval mode updated to `{new_mode}`",
                    },
                }
            ],
        )
