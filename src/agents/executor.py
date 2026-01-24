"""Agent executor for running agents with proper restrictions."""

import asyncio
from datetime import datetime
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from loguru import logger

from ..claude.subprocess_executor import SubprocessExecutor
from .models import (
    AgentConfig,
    AgentExecution,
    AgentExecutionStatus,
    AgentModelChoice,
    AgentPermissionMode,
    AgentRunResult,
)


class AgentExecutor:
    """Executes agents with proper tool and model restrictions.

    Each agent runs in its own subprocess session with isolated context.
    """

    def __init__(self, subprocess_executor: SubprocessExecutor) -> None:
        """Initialize executor.

        Parameters
        ----------
        subprocess_executor : SubprocessExecutor
            The underlying Claude executor.
        """
        self.subprocess_executor = subprocess_executor
        self._active_executions: dict[str, AgentExecution] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}

    async def run(
        self,
        agent: AgentConfig,
        task: str,
        working_directory: str,
        channel_id: str,
        thread_ts: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        parent_permission_mode: Optional[str] = None,
        parent_model: Optional[str] = None,
        on_status_update: Optional[Callable[[AgentExecution], Awaitable[None]]] = None,
        run_in_background: bool = False,
    ) -> AgentRunResult:
        """Execute an agent with the given task.

        Parameters
        ----------
        agent : AgentConfig
            The agent configuration to execute.
        task : str
            The task/prompt to send to the agent.
        working_directory : str
            Working directory for execution.
        channel_id : str
            Slack channel ID.
        thread_ts : str, optional
            Slack thread timestamp.
        parent_session_id : str, optional
            Claude session ID from parent (for context).
        parent_permission_mode : str, optional
            Permission mode from parent session.
        parent_model : str, optional
            Model from parent session.
        on_status_update : callable, optional
            Callback for status updates.
        run_in_background : bool
            If True, run in background and return immediately.

        Returns
        -------
        AgentRunResult
            Execution outcome.
        """
        execution_id = str(uuid4())[:8]

        execution = AgentExecution(
            execution_id=execution_id,
            agent_name=agent.name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            task_description=task[:200],
            working_directory=working_directory,
            status=AgentExecutionStatus.PENDING,
            run_in_background=run_in_background,
        )

        self._active_executions[execution_id] = execution

        if run_in_background:
            task_future = asyncio.create_task(
                self._execute_agent(
                    execution,
                    agent,
                    task,
                    parent_session_id,
                    parent_permission_mode,
                    parent_model,
                    on_status_update,
                )
            )
            self._background_tasks[execution_id] = task_future

            return AgentRunResult(
                execution_id=execution_id,
                agent_name=agent.name,
                success=True,
                output=f"Agent '{agent.name}' started in background (ID: {execution_id})",
            )
        else:
            return await self._execute_agent(
                execution,
                agent,
                task,
                parent_session_id,
                parent_permission_mode,
                parent_model,
                on_status_update,
            )

    async def _execute_agent(
        self,
        execution: AgentExecution,
        agent: AgentConfig,
        task: str,
        parent_session_id: Optional[str],
        parent_permission_mode: Optional[str],
        parent_model: Optional[str],
        on_status_update: Optional[Callable[[AgentExecution], Awaitable[None]]],
    ) -> AgentRunResult:
        """Internal execution logic.

        Parameters
        ----------
        execution : AgentExecution
            Execution tracking object.
        agent : AgentConfig
            Agent configuration.
        task : str
            Task to execute.
        parent_session_id : str, optional
            Parent Claude session ID.
        parent_permission_mode : str, optional
            Parent permission mode.
        parent_model : str, optional
            Parent model.
        on_status_update : callable, optional
            Status update callback.

        Returns
        -------
        AgentRunResult
            Execution result.
        """
        execution.status = AgentExecutionStatus.RUNNING
        execution.started_at = datetime.now()

        if on_status_update:
            await on_status_update(execution)

        try:
            full_prompt = self._build_prompt(agent, task)
            model = self._resolve_model(agent, parent_model)
            permission_mode = self._resolve_permission_mode(agent, parent_permission_mode)

            logger.info(
                f"Executing agent '{agent.name}' (model={model}, mode={permission_mode})"
            )

            result = await self.subprocess_executor.execute(
                prompt=full_prompt,
                working_directory=execution.working_directory,
                session_id=f"agent-{execution.execution_id}",
                execution_id=execution.execution_id,
                permission_mode=permission_mode,
                model=model,
            )

            execution.session_id = result.session_id
            execution.output = result.output
            execution.turn_count = 1

            if result.success:
                execution.status = AgentExecutionStatus.COMPLETED
            else:
                execution.status = AgentExecutionStatus.FAILED
                execution.error = result.error

            execution.completed_at = datetime.now()

            if on_status_update:
                await on_status_update(execution)

            return AgentRunResult(
                execution_id=execution.execution_id,
                agent_name=agent.name,
                success=result.success,
                output=result.output,
                detailed_output=result.detailed_output,
                error=result.error,
                session_id=result.session_id,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                turn_count=execution.turn_count,
            )

        except asyncio.CancelledError:
            execution.status = AgentExecutionStatus.CANCELLED
            execution.completed_at = datetime.now()
            raise

        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            execution.status = AgentExecutionStatus.FAILED
            execution.error = str(e)
            execution.completed_at = datetime.now()

            return AgentRunResult(
                execution_id=execution.execution_id,
                agent_name=agent.name,
                success=False,
                output="",
                error=str(e),
            )

        finally:
            self._active_executions.pop(execution.execution_id, None)
            self._background_tasks.pop(execution.execution_id, None)

    def _build_prompt(self, agent: AgentConfig, task: str) -> str:
        """Build the full prompt including agent's system prompt.

        Parameters
        ----------
        agent : AgentConfig
            Agent configuration.
        task : str
            User task.

        Returns
        -------
        str
            Complete prompt for execution.
        """
        if agent.system_prompt:
            return f"{agent.system_prompt}\n\n---\n\nTask:\n{task}"
        return task

    def _resolve_model(
        self, agent: AgentConfig, parent_model: Optional[str]
    ) -> Optional[str]:
        """Resolve the model to use for execution.

        Parameters
        ----------
        agent : AgentConfig
            Agent configuration.
        parent_model : str, optional
            Parent session's model.

        Returns
        -------
        str or None
            Model to use.
        """
        if agent.model == AgentModelChoice.INHERIT:
            return parent_model
        return agent.model.value

    def _resolve_permission_mode(
        self, agent: AgentConfig, parent_mode: Optional[str]
    ) -> Optional[str]:
        """Resolve the permission mode for execution.

        Parameters
        ----------
        agent : AgentConfig
            Agent configuration.
        parent_mode : str, optional
            Parent session's permission mode.

        Returns
        -------
        str or None
            Permission mode to use.
        """
        if agent.permission_mode == AgentPermissionMode.INHERIT:
            return parent_mode
        return agent.permission_mode.value

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution.

        Parameters
        ----------
        execution_id : str
            ID of execution to cancel.

        Returns
        -------
        bool
            True if cancelled successfully.
        """
        cancelled = await self.subprocess_executor.cancel(execution_id)

        task = self._background_tasks.pop(execution_id, None)
        if task and not task.done():
            task.cancel()
            return True

        return cancelled

    def get_active_executions(self) -> list[AgentExecution]:
        """Get all active agent executions.

        Returns
        -------
        list[AgentExecution]
            Currently running executions.
        """
        return list(self._active_executions.values())

    def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get a specific execution by ID.

        Parameters
        ----------
        execution_id : str
            Execution ID to look up.

        Returns
        -------
        AgentExecution or None
            The execution, or None if not found.
        """
        return self._active_executions.get(execution_id)

    async def resume(
        self,
        execution_id: str,
        follow_up_prompt: str,
        on_status_update: Optional[Callable[[AgentExecution], Awaitable[None]]] = None,
    ) -> AgentRunResult:
        """Resume a completed agent execution with additional input.

        Uses the stored session_id to continue the conversation.

        Parameters
        ----------
        execution_id : str
            ID of execution to resume.
        follow_up_prompt : str
            Additional prompt to continue with.
        on_status_update : callable, optional
            Status update callback.

        Returns
        -------
        AgentRunResult
            Result of resumed execution.
        """
        execution = self._active_executions.get(execution_id)
        if not execution or not execution.session_id:
            return AgentRunResult(
                execution_id=execution_id,
                agent_name="unknown",
                success=False,
                output="",
                error="Execution not found or has no session to resume",
            )

        result = await self.subprocess_executor.execute(
            prompt=follow_up_prompt,
            working_directory=execution.working_directory,
            session_id=f"agent-{execution_id}",
            resume_session_id=execution.session_id,
            execution_id=f"{execution_id}-resume",
        )

        return AgentRunResult(
            execution_id=execution_id,
            agent_name=execution.agent_name,
            success=result.success,
            output=result.output,
            detailed_output=result.detailed_output,
            error=result.error,
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        )
