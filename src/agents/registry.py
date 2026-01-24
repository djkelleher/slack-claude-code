"""Agent registry for discovering and managing agent configurations."""

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

from .builtin import BUILTIN_AGENTS
from .models import AgentConfig, AgentModelChoice, AgentPermissionMode, AgentSource


class AgentRegistry:
    """Discovers, loads, and manages agent configurations.

    Merges agents from three sources (priority order, higher overrides lower):
    1. Project agents from .claude/agents/ (highest priority)
    2. User agents from ~/.claude/agents/
    3. Built-in agents (lowest priority)
    """

    def __init__(self, working_directory: str = "~") -> None:
        """Initialize registry.

        Parameters
        ----------
        working_directory : str
            Working directory for project-level agents.
        """
        self.working_directory = os.path.expanduser(working_directory)
        self._agents: dict[str, AgentConfig] = {}
        self._loaded = False

    def load(self, force_reload: bool = False) -> None:
        """Load all agents from all sources.

        Parameters
        ----------
        force_reload : bool
            If True, reload even if already loaded.
        """
        if self._loaded and not force_reload:
            return

        self._agents.clear()

        # 1. Load built-in agents (lowest priority)
        for name, agent in BUILTIN_AGENTS.items():
            self._agents[name] = agent

        # 2. Load user agents from ~/.claude/agents/
        user_agents_dir = Path.home() / ".claude" / "agents"
        self._load_agents_from_dir(user_agents_dir, AgentSource.USER)

        # 3. Load project agents from .claude/agents/ (highest priority)
        project_agents_dir = Path(self.working_directory) / ".claude" / "agents"
        self._load_agents_from_dir(project_agents_dir, AgentSource.PROJECT)

        self._loaded = True
        logger.info(f"Loaded {len(self._agents)} agents")

    def _load_agents_from_dir(self, directory: Path, source: AgentSource) -> None:
        """Load agent definitions from a directory.

        Parameters
        ----------
        directory : Path
            Directory containing .md agent files.
        source : AgentSource
            Source type for loaded agents.
        """
        if not directory.exists():
            return

        for file_path in directory.glob("*.md"):
            try:
                agent = self._parse_agent_file(file_path, source)
                if agent:
                    self._agents[agent.name] = agent
                    logger.debug(f"Loaded agent '{agent.name}' from {file_path}")
            except Exception as e:
                logger.warning(f"Failed to load agent from {file_path}: {e}")

    def _parse_agent_file(
        self, file_path: Path, source: AgentSource
    ) -> Optional[AgentConfig]:
        """Parse a markdown agent file with YAML frontmatter.

        Parameters
        ----------
        file_path : Path
            Path to the .md agent file.
        source : AgentSource
            Source type for the agent.

        Returns
        -------
        AgentConfig or None
            Parsed agent config, or None if invalid.
        """
        content = file_path.read_text()

        # Split frontmatter and body
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n?(.*)", content, re.DOTALL)
        if not frontmatter_match:
            logger.warning(f"No frontmatter found in {file_path}")
            return None

        frontmatter_str = frontmatter_match.group(1)
        body = frontmatter_match.group(2).strip()

        try:
            frontmatter = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.warning(f"Invalid YAML in {file_path}: {e}")
            return None

        # Extract name from filename if not in frontmatter
        name = frontmatter.get("name", file_path.stem)

        # Validate required fields
        description = frontmatter.get("description", "")
        if not description:
            logger.warning(f"Agent '{name}' missing description in {file_path}")

        # Parse model choice
        model_str = frontmatter.get("model", "inherit")
        try:
            model = AgentModelChoice(model_str.lower())
        except ValueError:
            logger.warning(f"Invalid model '{model_str}' in {file_path}, using inherit")
            model = AgentModelChoice.INHERIT

        # Parse permission mode
        mode_str = frontmatter.get("permissionMode", "inherit")
        try:
            permission_mode = AgentPermissionMode(mode_str)
        except ValueError:
            logger.warning(f"Invalid permissionMode '{mode_str}' in {file_path}, using inherit")
            permission_mode = AgentPermissionMode.INHERIT

        # Parse tools lists
        tools = frontmatter.get("tools", [])
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",")]
        disallowed_tools = frontmatter.get("disallowedTools", [])
        if isinstance(disallowed_tools, str):
            disallowed_tools = [t.strip() for t in disallowed_tools.split(",")]

        return AgentConfig(
            name=name,
            description=description,
            source=source,
            file_path=str(file_path),
            system_prompt=body,
            tools=tools,
            disallowed_tools=disallowed_tools,
            model=model,
            permission_mode=permission_mode,
            max_turns=frontmatter.get("maxTurns", 50),
            is_builtin=False,
        )

    def get(self, name: str) -> Optional[AgentConfig]:
        """Get an agent by name.

        Parameters
        ----------
        name : str
            Agent name to look up.

        Returns
        -------
        AgentConfig or None
            The agent config, or None if not found.
        """
        self.load()
        return self._agents.get(name)

    def list_all(self) -> list[AgentConfig]:
        """Get all registered agents.

        Returns
        -------
        list[AgentConfig]
            All loaded agent configs.
        """
        self.load()
        return list(self._agents.values())

    def list_by_source(self, source: AgentSource) -> list[AgentConfig]:
        """Get agents from a specific source.

        Parameters
        ----------
        source : AgentSource
            Source type to filter by.

        Returns
        -------
        list[AgentConfig]
            Agents matching the source.
        """
        self.load()
        return [a for a in self._agents.values() if a.source == source]

    def select_for_task(self, task_description: str) -> AgentConfig:
        """Select the best agent for a task based on descriptions.

        Uses simple keyword matching. For production, consider
        using an LLM to select the best agent.

        Parameters
        ----------
        task_description : str
            The task to find an agent for.

        Returns
        -------
        AgentConfig
            The selected agent (defaults to general).
        """
        self.load()

        task_lower = task_description.lower()

        # Simple heuristic selection based on keywords
        if any(kw in task_lower for kw in ["explore", "find", "search", "understand", "investigate", "where", "how does"]):
            if "explore" in self._agents:
                return self._agents["explore"]

        if any(kw in task_lower for kw in ["plan", "design", "architect", "strategy"]):
            if "plan" in self._agents:
                return self._agents["plan"]

        if any(kw in task_lower for kw in ["git", "npm", "yarn", "pip", "bash", "shell", "run", "execute", "build", "test"]):
            if "bash" in self._agents:
                return self._agents["bash"]

        # Default to general agent
        return self._agents.get("general", list(self._agents.values())[0])

    def add(self, agent: AgentConfig) -> None:
        """Add a custom agent (e.g., created programmatically).

        Parameters
        ----------
        agent : AgentConfig
            Agent to add to the registry.
        """
        self._agents[agent.name] = agent


def get_registry(working_directory: str = "~") -> AgentRegistry:
    """Get an agent registry for the given working directory.

    Creates a new registry instance each time to ensure fresh
    project-level agent loading.

    Parameters
    ----------
    working_directory : str
        Working directory for project agents.

    Returns
    -------
    AgentRegistry
        Registry with all agents loaded.
    """
    registry = AgentRegistry(working_directory)
    registry.load()
    return registry
