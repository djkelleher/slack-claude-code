"""Unit tests for the agent registry."""

from pathlib import Path

import pytest

import src.agents.registry as registry_module
from src.agents.models import (
    AgentConfig,
    AgentModelChoice,
    AgentPermissionMode,
    AgentSource,
)
from src.agents.registry import AgentRegistry, get_registry


def _write_agent_file(path: Path, content: str) -> None:
    """Write an agent markdown file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_load_merges_sources_with_project_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Project agents should override user agents and built-ins."""
    user_home = tmp_path / "home"
    project_dir = tmp_path / "workspace"

    monkeypatch.setattr(registry_module.Path, "home", staticmethod(lambda: user_home))

    _write_agent_file(
        user_home / ".claude" / "agents" / "general.md",
        """---
description: User general override
tools: Read, Bash
---
User prompt
""",
    )
    _write_agent_file(
        user_home / ".claude" / "agents" / "research.md",
        """---
description: User research agent
---
Research prompt
""",
    )
    _write_agent_file(
        project_dir / ".claude" / "agents" / "general.md",
        """---
description: Project general override
model: haiku
---
Project prompt
""",
    )
    _write_agent_file(
        project_dir / ".claude" / "agents" / "delivery.md",
        """---
description: Project delivery agent
maxTurns: 12
---
Delivery prompt
""",
    )

    registry = AgentRegistry(str(project_dir))
    registry.load()

    general = registry.get("general")
    assert general is not None
    assert general.description == "Project general override"
    assert general.source == AgentSource.PROJECT
    assert general.model == AgentModelChoice.HAIKU
    assert general.system_prompt == "Project prompt"

    research = registry.get("research")
    assert research is not None
    assert research.source == AgentSource.USER

    delivery = registry.get("delivery")
    assert delivery is not None
    assert delivery.max_turns == 12
    assert delivery.source == AgentSource.PROJECT

    user_agents = registry.list_by_source(AgentSource.USER)
    assert [agent.name for agent in user_agents] == ["research"]

    project_agents = {agent.name for agent in registry.list_by_source(AgentSource.PROJECT)}
    assert project_agents == {"general", "delivery"}

    all_agents = {agent.name for agent in registry.list_all()}
    assert {"general", "explore", "plan", "bash", "research", "delivery"} <= all_agents


def test_parse_agent_file_normalizes_lists_and_invalid_frontmatter(tmp_path: Path):
    """Invalid model metadata should fall back to inherit and string lists should split."""
    agent_file = tmp_path / "custom.md"
    _write_agent_file(
        agent_file,
        """---
description: Custom agent
tools: Read, Bash , Edit
disallowedTools: Write, Grep
model: INVALID
permissionMode: nope
maxTurns: 9
---

Custom system prompt
""",
    )

    registry = AgentRegistry(str(tmp_path))
    agent = registry._parse_agent_file(agent_file, AgentSource.PROJECT)

    assert agent is not None
    assert agent.name == "custom"
    assert agent.description == "Custom agent"
    assert agent.tools == ["Read", "Bash", "Edit"]
    assert agent.disallowed_tools == ["Write", "Grep"]
    assert agent.model == AgentModelChoice.INHERIT
    assert agent.permission_mode == AgentPermissionMode.INHERIT
    assert agent.max_turns == 9
    assert agent.system_prompt == "Custom system prompt"
    assert agent.file_path == str(agent_file)
    assert agent.is_builtin is False


def test_parse_agent_file_returns_none_for_missing_frontmatter_or_invalid_yaml(
    tmp_path: Path,
):
    """Malformed agent files should be ignored."""
    no_frontmatter = tmp_path / "plain.md"
    invalid_yaml = tmp_path / "broken.md"

    _write_agent_file(no_frontmatter, "No frontmatter here")
    _write_agent_file(
        invalid_yaml,
        """---
description: [unterminated
---
Body
""",
    )

    registry = AgentRegistry(str(tmp_path))

    assert registry._parse_agent_file(no_frontmatter, AgentSource.PROJECT) is None
    assert registry._parse_agent_file(invalid_yaml, AgentSource.PROJECT) is None


def test_load_uses_cache_until_force_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """load() should skip reloading unless force_reload is requested."""
    user_home = tmp_path / "home"
    project_dir = tmp_path / "workspace"

    monkeypatch.setattr(registry_module.Path, "home", staticmethod(lambda: user_home))

    registry = AgentRegistry(str(project_dir))
    registry.load()
    registry.add(
        AgentConfig(
            name="temporary",
            description="Added after initial load",
            source=AgentSource.PROJECT,
            system_prompt="Temp",
        )
    )

    assert registry.get("temporary") is not None

    registry.load()
    assert registry.get("temporary") is not None

    registry.load(force_reload=True)
    assert registry.get("temporary") is None


@pytest.mark.parametrize(
    ("task_description", "expected_agent"),
    [
        ("Find where the queue state is built", "explore"),
        ("Design an architecture plan for notifications", "plan"),
        ("Run tests and execute a shell command", "bash"),
        ("Implement the feature end to end", "general"),
    ],
)
def test_select_for_task_uses_builtin_heuristics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_description: str,
    expected_agent: str,
):
    """Keyword heuristics should choose the expected built-in agent."""
    monkeypatch.setattr(registry_module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    registry = AgentRegistry(str(tmp_path / "workspace"))

    selected = registry.select_for_task(task_description)

    assert selected.name == expected_agent


def test_select_for_task_raises_when_no_agents_are_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty registry should raise a clear error when selecting an agent."""
    monkeypatch.setattr(registry_module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(registry_module, "BUILTIN_AGENTS", {})

    registry = AgentRegistry(str(tmp_path / "workspace"))

    with pytest.raises(RuntimeError, match="No agents registered"):
        registry.select_for_task("do something")


def test_get_registry_loads_agents_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """get_registry() should return a loaded registry instance."""
    user_home = tmp_path / "home"
    project_dir = tmp_path / "workspace"

    monkeypatch.setattr(registry_module.Path, "home", staticmethod(lambda: user_home))
    _write_agent_file(
        project_dir / ".claude" / "agents" / "custom.md",
        """---
description: Project custom agent
permissionMode: acceptEdits
---
Custom prompt
""",
    )

    registry = get_registry(str(project_dir))

    custom = registry.get("custom")
    assert custom is not None
    assert registry._loaded is True
    assert custom.permission_mode == AgentPermissionMode.ACCEPT_EDITS
