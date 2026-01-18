"""Multi-agent workflow orchestrator.

Coordinates Planner -> Worker -> Evaluator pipeline for complex tasks.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Optional

from loguru import logger

from ..claude.subprocess_executor import ExecutionResult, SubprocessExecutor
from .roles import AgentRole, format_task_prompt


class TaskStatus(Enum):
    """Status of an agent task."""

    PENDING = "pending"
    PLANNING = "planning"
    WORKING = "working"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvalResult(Enum):
    """Evaluation result from evaluator agent."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass
class AgentTask:
    """A task being processed by the multi-agent workflow."""

    task_id: str
    description: str
    channel_id: str
    working_directory: str = "~"
    status: TaskStatus = TaskStatus.PENDING
    plan_output: Optional[str] = None
    work_output: Optional[str] = None
    eval_output: Optional[str] = None
    eval_result: Optional[EvalResult] = None
    error_message: Optional[str] = None
    turn_count: int = 0
    max_turns: int = 50
    slack_thread_ts: Optional[str] = None
    message_ts: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class WorkflowResult:
    """Result of a complete workflow execution."""

    task: AgentTask
    success: bool
    plan: Optional[str] = None
    work_output: Optional[str] = None
    evaluation: Optional[str] = None
    eval_result: Optional[EvalResult] = None
    total_turns: int = 0
    duration_ms: Optional[int] = None


class MultiAgentOrchestrator:
    """Orchestrates multi-agent workflows.

    Runs tasks through Planner -> Worker -> Evaluator pipeline.
    Each agent uses its own isolated subprocess session.
    """

    def __init__(
        self,
        executor: SubprocessExecutor,
        max_iterations: int = 3,
    ) -> None:
        """Initialize orchestrator.

        Args:
            executor: Claude executor for running agents
            max_iterations: Max worker-evaluator iterations
        """
        self.executor = executor
        self.max_iterations = max_iterations
        self._active_tasks: dict[str, AgentTask] = {}

    async def execute_workflow(
        self,
        task: AgentTask,
        on_status_update: Optional[Callable[[AgentTask], Awaitable[None]]] = None,
    ) -> WorkflowResult:
        """Execute the full multi-agent workflow.

        Args:
            task: The task to execute
            on_status_update: Optional callback for status updates

        Returns:
            WorkflowResult with outcomes
        """
        self._active_tasks[task.task_id] = task
        task.started_at = datetime.now()
        start_time = asyncio.get_running_loop().time()

        try:
            # Phase 1: Planning
            task.status = TaskStatus.PLANNING
            if on_status_update:
                await on_status_update(task)

            plan_result = await self._run_planner(task)
            if not plan_result.success:
                task.status = TaskStatus.FAILED
                task.error_message = plan_result.error
                return self._create_result(task, False, start_time)

            task.plan_output = plan_result.output
            task.turn_count += 1

            # Phase 2: Working (with potential re-iterations)
            for iteration in range(self.max_iterations):
                task.status = TaskStatus.WORKING
                if on_status_update:
                    await on_status_update(task)

                work_result = await self._run_worker(task)
                if not work_result.success:
                    task.status = TaskStatus.FAILED
                    task.error_message = work_result.error
                    return self._create_result(task, False, start_time)

                task.work_output = work_result.output
                task.turn_count += 1

                # Phase 3: Evaluation
                task.status = TaskStatus.EVALUATING
                if on_status_update:
                    await on_status_update(task)

                eval_result = await self._run_evaluator(task)
                task.eval_output = eval_result.output
                task.turn_count += 1

                # Parse evaluation result
                eval_verdict = self._parse_eval_result(eval_result.output)
                task.eval_result = eval_verdict

                if eval_verdict == EvalResult.COMPLETE:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = datetime.now()
                    return self._create_result(task, True, start_time)

                elif eval_verdict == EvalResult.FAILED:
                    task.status = TaskStatus.FAILED
                    task.error_message = "Evaluation returned FAILED"
                    return self._create_result(task, False, start_time)

                # For PARTIAL or INCOMPLETE, continue to next iteration
                logger.info(
                    f"Task {task.task_id} eval: {eval_verdict.value}, "
                    f"iteration {iteration + 1}/{self.max_iterations}"
                )

            # Max iterations reached
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            return self._create_result(task, True, start_time)

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            raise

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            logger.error(f"Workflow failed for task {task.task_id}: {e}")
            return self._create_result(task, False, start_time)

        finally:
            self._active_tasks.pop(task.task_id, None)
            # Cleanup agent sessions
            await self._cleanup_sessions(task.task_id)

    async def _run_planner(self, task: AgentTask) -> ExecutionResult:
        """Run the planner agent."""
        prompt = format_task_prompt(
            role=AgentRole.PLANNER,
            task=task.description,
            working_directory=task.working_directory,
        )

        # Use task-specific session for planner
        session_id = f"{task.task_id}-planner"

        return await self.executor.execute(
            prompt=prompt,
            working_directory=task.working_directory,
            session_id=session_id,
            execution_id=session_id,
        )

    async def _run_worker(self, task: AgentTask) -> ExecutionResult:
        """Run the worker agent."""
        prompt = format_task_prompt(
            role=AgentRole.WORKER,
            task=task.description,
            plan=task.plan_output,
        )

        # Use task-specific session for worker
        session_id = f"{task.task_id}-worker"

        return await self.executor.execute(
            prompt=prompt,
            working_directory=task.working_directory,
            session_id=session_id,
            execution_id=session_id,
        )

    async def _run_evaluator(self, task: AgentTask) -> ExecutionResult:
        """Run the evaluator agent."""
        prompt = format_task_prompt(
            role=AgentRole.EVALUATOR,
            task=task.description,
            plan=task.plan_output,
            work_output=task.work_output,
        )

        # Use task-specific session for evaluator
        session_id = f"{task.task_id}-evaluator"

        return await self.executor.execute(
            prompt=prompt,
            working_directory=task.working_directory,
            session_id=session_id,
            execution_id=session_id,
        )

    def _parse_eval_result(self, output: str) -> EvalResult:
        """Parse evaluation verdict from output."""
        output_upper = output.upper()

        if "COMPLETE" in output_upper and "INCOMPLETE" not in output_upper:
            return EvalResult.COMPLETE
        elif "INCOMPLETE" in output_upper:
            return EvalResult.INCOMPLETE
        elif "PARTIAL" in output_upper:
            return EvalResult.PARTIAL
        elif "FAILED" in output_upper or "FAIL" in output_upper:
            return EvalResult.FAILED

        # Default to partial if can't determine
        return EvalResult.PARTIAL

    def _create_result(
        self,
        task: AgentTask,
        success: bool,
        start_time: float,
    ) -> WorkflowResult:
        """Create a workflow result."""
        duration_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)

        return WorkflowResult(
            task=task,
            success=success,
            plan=task.plan_output,
            work_output=task.work_output,
            evaluation=task.eval_output,
            eval_result=task.eval_result,
            total_turns=task.turn_count,
            duration_ms=duration_ms,
        )

    async def _cleanup_sessions(self, task_id: str) -> None:
        """Cancel any active subprocess executions for a task."""
        for role in ["planner", "worker", "evaluator"]:
            session_id = f"{task_id}-{role}"
            await self.executor.cancel(session_id)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel an active task.

        Args:
            task_id: The task ID to cancel

        Returns:
            True if task was found and cancelled
        """
        task = self._active_tasks.get(task_id)
        if not task:
            return False

        task.status = TaskStatus.CANCELLED
        await self._cleanup_sessions(task_id)
        return True

    def get_active_tasks(self) -> list[AgentTask]:
        """Get list of active tasks."""
        return list(self._active_tasks.values())

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        """Get a specific task by ID."""
        return self._active_tasks.get(task_id)
