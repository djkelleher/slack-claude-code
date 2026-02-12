"""Codex mode switching command handlers: /sandbox, /approval, /effort."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

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
                blocks=SlackFormatter.error_message(
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
            current_mode = session.approval_mode or config.CODEX_APPROVAL_MODE
            modes_list = "\n".join([f"• `{m}`" for m in config.VALID_APPROVAL_MODES])

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":clipboard: *Current approval mode:* `{current_mode}`\n\n*Available modes:*\n{modes_list}",
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

        if new_mode not in config.VALID_APPROVAL_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(
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

    @app.command("/effort")
    @slack_command()
    async def handle_effort(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Set reasoning effort level for the session (Codex)."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        # Map user-friendly names to config values
        effort_aliases = {
            "low": "low",
            "medium": "medium",
            "med": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "extra high": "xhigh",
            "extra-high": "xhigh",
            "extra_high": "xhigh",
        }

        # Display names for levels
        effort_display = {
            "low": "Low — Fast responses with lighter reasoning",
            "medium": "Medium — Balances speed and reasoning depth",
            "high": "High — Greater reasoning depth for complex problems",
            "xhigh": "Extra High — Maximum reasoning depth",
        }

        if not ctx.text:
            # Show current level and available options
            current_level = session.reasoning_effort or config.DEFAULT_REASONING_EFFORT
            levels_list = "\n".join(
                [f"• `{k}` — {v.split(' — ')[1]}" for k, v in effort_display.items()]
            )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":brain: *Current reasoning effort:* `{current_level}`\n\n*Available levels:*\n{levels_list}",
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/effort <level>` to change the reasoning effort. Only applies to Codex models.",
                            }
                        ],
                    },
                ],
            )
            return

        new_level = effort_aliases.get(ctx.text.lower().strip())

        if not new_level:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(
                    f"Invalid reasoning effort: `{ctx.text.strip()}`\n\n"
                    f"Valid levels: {', '.join(config.VALID_REASONING_LEVELS)}"
                ),
            )
            return

        await deps.db.update_session_reasoning_effort(ctx.channel_id, ctx.thread_ts, new_level)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":brain: Reasoning effort updated to `{new_level}`",
                    },
                }
            ],
        )
