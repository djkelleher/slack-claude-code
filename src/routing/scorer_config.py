"""Scoring dimensions and keyword lists for request complexity analysis.

Adapted from Manifest's scorer config
(packages/backend/src/routing/scorer/config.ts), filtered to dimensions
most relevant for coding-focused AI CLI tools.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DimensionConfig:
    """Configuration for a single scoring dimension.

    Parameters
    ----------
    name : str
        Dimension identifier.
    weight : float
        Contribution weight (all weights should sum to ~1.0).
    direction : str
        "up" means matches increase complexity, "down" means they decrease it.
    keywords : tuple[str, ...]
        Keyword phrases to match. Empty for structural dimensions.
    """

    name: str
    weight: float
    direction: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class TierBoundaries:
    """Score thresholds for tier assignment.

    Adapted from Manifest's sigmoid boundaries
    (simpleMax=-0.10, standardMax=0.08, complexMax=0.35).
    """

    simple_max: float = -0.10
    standard_max: float = 0.10
    complex_max: float = 0.35


# Keyword lists adapted from Manifest's DEFAULT_KEYWORDS,
# focused on coding-relevant dimensions.

SIMPLE_INDICATORS: tuple[str, ...] = (
    "what is",
    "define",
    "translate",
    "thanks",
    "thank you",
    "yes",
    "no",
    "ok",
    "okay",
    "sure",
    "got it",
    "hi",
    "hello",
    "hey",
    "bye",
    "goodbye",
    "how are you",
    "good morning",
    "please",
    "help",
    "where is",
    "who is",
)

CODE_GENERATION: tuple[str, ...] = (
    "write a function",
    "implement",
    "create a class",
    "build a component",
    "write code",
    "write a script",
    "code this",
    "create an api",
    "build a module",
    "scaffold",
    "boilerplate",
    "write a test",
    "write tests",
    "generate code",
    "endpoint",
    "handler",
    "controller",
    "new feature",
    "add feature",
    "implementing",
    "implementation",
)

CODE_REVIEW: tuple[str, ...] = (
    "fix this bug",
    "debug",
    "why does this fail",
    "review this code",
    "what's wrong with",
    "code review",
    "refactor",
    "optimize this code",
    "find the error",
    "stack trace",
    "exception",
    "memory leak",
    "race condition",
    "deadlock",
    "off by one",
    "typeerror",
    "vulnerability",
)

MULTI_STEP: tuple[str, ...] = (
    "first",
    "then",
    "after that",
    "finally",
    "step 1",
    "step 2",
    "step 3",
    "next",
    "followed by",
    "phase 1",
    "phase 2",
    "workflow",
    "pipeline",
    "across files",
    "multiple files",
)

AGENTIC_TASKS: tuple[str, ...] = (
    "investigate",
    "migrate",
    "audit",
    "scan all",
    "check all",
    "review all",
    "update all",
    "rename across",
    "refactor all",
    "remediation",
    "batch process",
    "orchestrate",
)

REASONING: tuple[str, ...] = (
    "why",
    "root cause",
    "architecture",
    "design",
    "trade-offs",
    "tradeoffs",
    "pros and cons",
    "implications",
    "ramifications",
    "how does x relate",
    "what are the implications",
    "critically analyze",
    "compare",
    "evaluate",
    "assess",
)

# All scoring dimensions with their weights.
# Keyword dimensions use trie-based matching; structural dimensions
# use direct text analysis.
SCORING_DIMENSIONS: tuple[DimensionConfig, ...] = (
    DimensionConfig(
        name="simple_indicators",
        weight=0.15,
        direction="down",
        keywords=SIMPLE_INDICATORS,
    ),
    DimensionConfig(
        name="code_generation",
        weight=0.12,
        direction="up",
        keywords=CODE_GENERATION,
    ),
    DimensionConfig(
        name="code_review",
        weight=0.10,
        direction="up",
        keywords=CODE_REVIEW,
    ),
    DimensionConfig(
        name="multi_step",
        weight=0.12,
        direction="up",
        keywords=MULTI_STEP,
    ),
    DimensionConfig(
        name="agentic_tasks",
        weight=0.10,
        direction="up",
        keywords=AGENTIC_TASKS,
    ),
    DimensionConfig(
        name="reasoning",
        weight=0.10,
        direction="up",
        keywords=REASONING,
    ),
    # Structural dimensions (no keywords — scored by text analysis)
    DimensionConfig(name="prompt_length", weight=0.10, direction="up"),
    DimensionConfig(name="file_references", weight=0.08, direction="up"),
    DimensionConfig(name="specificity", weight=0.08, direction="down"),
    DimensionConfig(name="constraint_density", weight=0.05, direction="up"),
)

DEFAULT_BOUNDARIES: TierBoundaries = TierBoundaries()

# Short message threshold (from Manifest's scorer/index.ts)
SHORT_MESSAGE_THRESHOLD: int = 50

# Long prompt floor (prompts longer than this get floored at COMPLEX)
LONG_PROMPT_THRESHOLD: int = 5000

# Confidence sigmoid parameters (from Manifest's config)
CONFIDENCE_K: float = 8.0
CONFIDENCE_MIDPOINT: float = 0.15
CONFIDENCE_THRESHOLD: float = 0.45
