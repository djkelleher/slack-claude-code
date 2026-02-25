"""Backend-aware command execution helpers."""

from dataclasses import dataclass
from typing import Any, Optional

from src.codex.capabilities import apply_codex_mode_to_prompt
from src.config import config
from src.database.models import Session


@dataclass
class CommandRouteResult:
    """Execution result annotated with the selected backend."""

    backend: str
    result: Any


def resolve_backend_for_session(session: Session) -> str:
    """Resolve backend for a session based on selected model."""
    return session.get_backend()


async def execute_for_session(
    deps: Any,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    execution_id: str,
    on_chunk: Any = None,
) -> CommandRouteResult:
    """Execute a prompt with the correct backend and persist resumed session IDs."""
    backend = resolve_backend_for_session(session)

    if backend == "codex":
        if not deps.codex_executor:
            raise RuntimeError("Codex executor is not configured")

        execution_prompt = apply_codex_mode_to_prompt(prompt, session.permission_mode)
        result = await deps.codex_executor.execute(
            prompt=execution_prompt,
            working_directory=session.working_directory,
            session_id=channel_id,
            resume_session_id=session.codex_session_id,
            execution_id=execution_id,
            on_chunk=on_chunk,
            sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
            db_session_id=session.id,
            model=session.model,
            channel_id=channel_id,
        )

        if result.session_id:
            await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)

        return CommandRouteResult(backend=backend, result=result)

    result = await deps.executor.execute(
        prompt=prompt,
        working_directory=session.working_directory,
        session_id=channel_id,
        resume_session_id=session.claude_session_id,
        execution_id=execution_id,
        on_chunk=on_chunk,
        permission_mode=session.permission_mode,
        db_session_id=session.id,
        model=session.model,
        channel_id=channel_id,
    )

    if result.session_id:
        await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

    return CommandRouteResult(backend=backend, result=result)
