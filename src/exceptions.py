"""Custom exception hierarchy for slack-claude-code.

This module defines a structured exception hierarchy for consistent
error handling across the application.
"""

from typing import Optional


class SlackClaudeError(Exception):
    """Base exception for all slack-claude-code errors.

    All custom exceptions inherit from this class for consistent catching.
    """

    def __init__(self, message: str, **context) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{self.message} ({context_str})"
        return self.message


# Execution Errors


class ExecutionError(SlackClaudeError):
    """Base exception for command execution failures."""

    def __init__(
        self,
        message: str,
        session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        **context,
    ) -> None:
        super().__init__(message, session_id=session_id, execution_id=execution_id, **context)
        self.session_id = session_id
        self.execution_id = execution_id


class ExecutionTimeoutError(ExecutionError):
    """Raised when command execution times out."""

    def __init__(
        self,
        message: str = "Command execution timed out",
        timeout: Optional[float] = None,
        **context,
    ) -> None:
        super().__init__(message, timeout=timeout, **context)
        self.timeout = timeout


class ProcessError(ExecutionError):
    """Raised when the underlying process crashes or terminates unexpectedly."""

    def __init__(
        self,
        message: str = "Process terminated unexpectedly",
        exit_code: Optional[int] = None,
        **context,
    ) -> None:
        super().__init__(message, exit_code=exit_code, **context)
        self.exit_code = exit_code


class ExecutionCancelledError(ExecutionError):
    """Raised when command execution is cancelled by user."""

    def __init__(
        self,
        message: str = "Execution was cancelled",
        **context,
    ) -> None:
        super().__init__(message, **context)


# Session Errors


class SessionError(SlackClaudeError):
    """Base exception for PTY session failures."""

    def __init__(
        self,
        message: str,
        session_id: Optional[str] = None,
        **context,
    ) -> None:
        super().__init__(message, session_id=session_id, **context)
        self.session_id = session_id


class SessionNotFoundError(SessionError):
    """Raised when a session cannot be found."""

    def __init__(
        self,
        session_id: str,
        message: str = "Session not found",
        **context,
    ) -> None:
        super().__init__(message, session_id=session_id, **context)


class SessionBusyError(SessionError):
    """Raised when a session is busy processing another command."""

    def __init__(
        self,
        session_id: str,
        message: str = "Session is busy",
        **context,
    ) -> None:
        super().__init__(message, session_id=session_id, **context)


class SessionStateError(SessionError):
    """Raised when a session is in an invalid state for the requested operation."""

    def __init__(
        self,
        session_id: str,
        current_state: str,
        expected_states: Optional[list[str]] = None,
        message: str = "Invalid session state",
        **context,
    ) -> None:
        super().__init__(
            message,
            session_id=session_id,
            current_state=current_state,
            expected_states=expected_states,
            **context,
        )
        self.current_state = current_state
        self.expected_states = expected_states


# Approval Errors


class ApprovalError(SlackClaudeError):
    """Base exception for permission approval failures."""

    def __init__(
        self,
        message: str,
        approval_id: Optional[str] = None,
        **context,
    ) -> None:
        super().__init__(message, approval_id=approval_id, **context)
        self.approval_id = approval_id


class ApprovalTimeoutError(ApprovalError):
    """Raised when a permission approval request times out."""

    def __init__(
        self,
        approval_id: str,
        timeout: Optional[float] = None,
        message: str = "Approval request timed out",
        **context,
    ) -> None:
        super().__init__(message, approval_id=approval_id, timeout=timeout, **context)
        self.timeout = timeout


class ApprovalNotFoundError(ApprovalError):
    """Raised when an approval request cannot be found."""

    def __init__(
        self,
        approval_id: str,
        message: str = "Approval request not found",
        **context,
    ) -> None:
        super().__init__(message, approval_id=approval_id, **context)


class StaleApprovalError(ApprovalError):
    """Raised when trying to respond to a stale approval request."""

    def __init__(
        self,
        approval_id: str,
        message: str = "Approval request is stale",
        **context,
    ) -> None:
        super().__init__(message, approval_id=approval_id, **context)


# Database Errors


class DatabaseError(SlackClaudeError):
    """Base exception for database operations."""

    pass


class DatabaseTimeoutError(DatabaseError):
    """Raised when a database operation times out."""

    def __init__(
        self,
        message: str = "Database operation timed out",
        operation: Optional[str] = None,
        **context,
    ) -> None:
        super().__init__(message, operation=operation, **context)
        self.operation = operation
