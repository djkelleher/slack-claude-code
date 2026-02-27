"""Session formatting helpers for Codex session management commands."""

from src.database.models import Session

from .base import escape_markdown, time_ago


def session_list(sessions: list[Session]) -> list[dict]:
    """Format a list of sessions for `/codex-sessions`."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":card_index: Sessions",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if not sessions:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No sessions found for this channel._"},
            }
        )
        return blocks

    for session in sessions[:20]:
        backend = session.get_backend()
        scope = "thread" if session.thread_ts else "channel"
        session_token = (
            session.codex_session_id if backend == "codex" else session.claude_session_id
        )
        active = ":white_check_mark:" if session_token else ":x:"
        model = session.model or "(default)"
        thread_label = session.thread_ts or "main"
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*ID*\n`{session.id}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Scope*\n`{scope}` (`{escape_markdown(thread_label)}`)",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Backend*\n`{backend}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Model*\n`{escape_markdown(model)}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Active Session*\n{active}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Last Active*\n{time_ago(session.last_active)}",
                    },
                ],
            }
        )

    if len(sessions) > 20:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Showing 20 of {len(sessions)} sessions._",
                    }
                ],
            }
        )

    return blocks


def session_cleanup_result(deleted_count: int, days: int) -> list[dict]:
    """Format result for `/codex-cleanup`."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":wastebasket: Removed *{deleted_count}* inactive session(s) "
                    f"older than *{days}* day(s)."
                ),
            },
        }
    ]
