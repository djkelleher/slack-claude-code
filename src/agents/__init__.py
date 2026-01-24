"""Agents module - configurable subagent system.

Provides both the new configurable agent system and the legacy
multi-agent orchestrator for backwards compatibility.
"""

from .executor import AgentExecutor
from .models import (
    AgentConfig,
    AgentExecution,
    AgentExecutionStatus,
    AgentModelChoice,
    AgentPermissionMode,
    AgentRunResult,
    AgentSource,
)
from .orchestrator import (
    AgentTask,
    EvalResult,
    MultiAgentOrchestrator,
    TaskStatus,
    WorkflowResult,
)
from .registry import AgentRegistry, get_registry
from .roles import AgentRole
