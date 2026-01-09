"""Session management formatting."""

from src.database.models import Session

from .base import time_ago


def session_list(sessions: list[Session]) -> list[dict]:
    """Format list of sessions for /sessions command.

    Parameters
    ----------
    sessions : list[Session]
        List of sessions to display.
    """
    if not sessions:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":clipboard: No sessions found for this channel.",
                },
            }
        ]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":clipboard: Sessions",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    # Separate channel and thread sessions
    channel_sessions = [s for s in sessions if not s.is_thread_session()]
    thread_sessions = [s for s in sessions if s.is_thread_session()]

    # Channel-level sessions first
    if channel_sessions:
        for session in channel_sessions:
            last_active_str = time_ago(session.last_active)
            created_str = session.created_at.strftime("%Y-%m-%d %H:%M")

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":file_folder: *Channel Session* (ID: {session.id})\n"
                        f"*Working Directory:* `{session.working_directory}`\n"
                        f"*Last Active:* {last_active_str}\n"
                        f"*Created:* {created_str}",
                    },
                }
            )

    # Thread sessions
    if thread_sessions:
        if channel_sessions:
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "*Thread Sessions*",
                        }
                    ],
                }
            )

        for session in thread_sessions[:20]:  # Limit to 20 threads to avoid message size issues
            last_active_str = time_ago(session.last_active)
            created_str = session.created_at.strftime("%Y-%m-%d %H:%M")

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":speech_balloon: *Thread* `{session.thread_ts}` (ID: {session.id})\n"
                        f"*Working Directory:* `{session.working_directory}`\n"
                        f"*Last Active:* {last_active_str}\n"
                        f"*Created:* {created_str}",
                    },
                }
            )

        if len(thread_sessions) > 20:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_... and {len(thread_sessions) - 20} more thread sessions_",
                        }
                    ],
                }
            )

    return blocks


def session_cleanup_result(deleted_count: int, inactive_days: int) -> list[dict]:
    """Format session cleanup result message.

    Parameters
    ----------
    deleted_count : int
        Number of sessions deleted.
    inactive_days : int
        Inactivity threshold used.
    """
    if deleted_count == 0:
        message = f":heavy_check_mark: No inactive sessions found (inactive >{inactive_days} days)."
    else:
        message = (
            f":wastebasket: Cleaned up {deleted_count} session(s) "
            f"that were inactive for more than {inactive_days} days."
        )

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": message,
            },
        }
    ]
