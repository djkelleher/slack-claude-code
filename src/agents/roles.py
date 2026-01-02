"""Agent roles and prompts for multi-agent workflow."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentRole(Enum):
    """Roles in the multi-agent workflow."""

    PLANNER = "planner"
    WORKER = "worker"
    EVALUATOR = "evaluator"


@dataclass
class AgentConfig:
    """Configuration for an agent role."""

    role: AgentRole
    max_turns: int
    system_prompt: str
    task_prompt_template: str


PLANNER_SYSTEM_PROMPT = """You are a planning agent. Your job is to:
1. Analyze the given task and break it down into clear, actionable steps
2. Identify potential risks and edge cases
3. Create a structured plan that a worker agent can follow

Be thorough but concise. Focus on clarity and actionability.
Output your plan in a structured format with numbered steps.
"""

PLANNER_TASK_TEMPLATE = """Task to plan: {task}

Context:
- Working directory: {working_directory}
- Available tools: Read, Write, Edit, Bash, Grep, Glob

Create a detailed plan for completing this task.
"""

WORKER_SYSTEM_PROMPT = """You are a worker agent. Your job is to:
1. Follow the plan provided by the planner
2. Execute each step carefully and thoroughly
3. Report your progress and any issues encountered
4. Produce high-quality work that meets the requirements

Focus on accuracy and completeness. If you encounter issues, document them clearly.
"""

WORKER_TASK_TEMPLATE = """Task: {task}

Plan to follow:
{plan}

Execute this plan step by step. Report your progress for each step.
"""

EVALUATOR_SYSTEM_PROMPT = """You are an evaluator agent. Your job is to:
1. Review the work completed by the worker
2. Check if all requirements were met
3. Identify any issues, bugs, or improvements needed
4. Provide a clear verdict: COMPLETE, PARTIAL, INCOMPLETE, or FAILED

Be objective and thorough. Focus on quality and correctness.
"""

EVALUATOR_TASK_TEMPLATE = """Original task: {task}

Plan that was followed:
{plan}

Work completed:
{work_output}

Evaluate this work:
1. Were all requirements met?
2. Is the quality acceptable?
3. Are there any issues or bugs?
4. What improvements are needed?

Provide your verdict: COMPLETE, PARTIAL, INCOMPLETE, or FAILED
"""


def get_agent_config(role: AgentRole, max_turns: Optional[int] = None) -> AgentConfig:
    """Get configuration for an agent role.

    Args:
        role: The agent role
        max_turns: Optional override for max turns

    Returns:
        AgentConfig for the role
    """
    from ..config import config

    configs = {
        AgentRole.PLANNER: AgentConfig(
            role=AgentRole.PLANNER,
            max_turns=max_turns or config.PLANNER_MAX_TURNS,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            task_prompt_template=PLANNER_TASK_TEMPLATE,
        ),
        AgentRole.WORKER: AgentConfig(
            role=AgentRole.WORKER,
            max_turns=max_turns or config.WORKER_MAX_TURNS,
            system_prompt=WORKER_SYSTEM_PROMPT,
            task_prompt_template=WORKER_TASK_TEMPLATE,
        ),
        AgentRole.EVALUATOR: AgentConfig(
            role=AgentRole.EVALUATOR,
            max_turns=max_turns or config.EVALUATOR_MAX_TURNS,
            system_prompt=EVALUATOR_SYSTEM_PROMPT,
            task_prompt_template=EVALUATOR_TASK_TEMPLATE,
        ),
    }

    return configs[role]


def format_task_prompt(
    role: AgentRole,
    task: str,
    working_directory: str = "~",
    plan: Optional[str] = None,
    work_output: Optional[str] = None,
) -> str:
    """Format a task prompt for an agent.

    Args:
        role: The agent role
        task: The task description
        working_directory: Working directory
        plan: Plan output (for worker/evaluator)
        work_output: Work output (for evaluator)

    Returns:
        Formatted prompt string
    """
    config = get_agent_config(role)

    if role == AgentRole.PLANNER:
        return config.task_prompt_template.format(
            task=task,
            working_directory=working_directory,
        )

    elif role == AgentRole.WORKER:
        return config.task_prompt_template.format(
            task=task,
            plan=plan or "No plan provided",
        )

    elif role == AgentRole.EVALUATOR:
        return config.task_prompt_template.format(
            task=task,
            plan=plan or "No plan provided",
            work_output=work_output or "No work output provided",
        )

    raise ValueError(f"Unknown role: {role}")
