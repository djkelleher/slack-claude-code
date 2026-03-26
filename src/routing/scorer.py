"""Request complexity scorer for smart model routing.

Adapted from Manifest's scorer (packages/backend/src/routing/scorer/).
Uses keyword trie matching for O(n) single-pass analysis and structural
text metrics, producing a tier recommendation with confidence score.
"""

import math
import re
from dataclasses import dataclass
from typing import Callable

from src.backends.models import ModelTier
from src.routing.scorer_config import (
    CONFIDENCE_K,
    CONFIDENCE_THRESHOLD,
    DEFAULT_BOUNDARIES,
    LONG_PROMPT_THRESHOLD,
    SCORING_DIMENSIONS,
    SHORT_MESSAGE_THRESHOLD,
    DimensionConfig,
    TierBoundaries,
)

# ---------------------------------------------------------------------------
# Keyword trie (adapted from Manifest's keyword-trie.ts)
# ---------------------------------------------------------------------------


def _is_word_char(c: str) -> bool:
    """Return True if character is alphanumeric or underscore."""
    code = ord(c)
    return (48 <= code <= 57) or (65 <= code <= 90) or (97 <= code <= 122) or code == 95


class _TrieNode:
    __slots__ = ("children", "terminals")

    def __init__(self) -> None:
        self.children: dict[str, "_TrieNode"] = {}
        self.terminals: list[tuple[str, str]] = []


class KeywordTrie:
    """Character-level trie with word-boundary detection for keyword matching."""

    MAX_SCAN_LENGTH = 100_000

    def __init__(self, dimensions: list[DimensionConfig]) -> None:
        self._root = _TrieNode()
        self._size = 0
        for dim in dimensions:
            for keyword in dim.keywords:
                self._insert(keyword.lower(), keyword.lower(), dim.name)

    def _insert(self, chars: str, keyword: str, dimension: str) -> None:
        node = self._root
        for ch in chars:
            if ch not in node.children:
                node.children[ch] = _TrieNode()
            node = node.children[ch]
        node.terminals.append((keyword, dimension))
        self._size += 1

    def scan(self, text: str) -> list[tuple[str, str, int]]:
        """Scan text for keyword matches with word-boundary enforcement.

        Returns
        -------
        list[tuple[str, str, int]]
            List of (keyword, dimension_name, position) matches.
        """
        matches: list[tuple[str, str, int]] = []
        lower = text.lower()
        length = min(len(lower), self.MAX_SCAN_LENGTH)

        i = 0
        while i < length:
            if i > 0 and _is_word_char(lower[i - 1]):
                i += 1
                continue

            node = self._root
            j = i
            while j < length:
                child = node.children.get(lower[j])
                if not child:
                    break
                node = child

                if node.terminals:
                    after_idx = j + 1
                    if after_idx < length and _is_word_char(lower[after_idx]):
                        j += 1
                        continue
                    for keyword, dimension in node.terminals:
                        matches.append((keyword, dimension, i))

                j += 1
            i += 1

        return matches

    @property
    def size(self) -> int:
        return self._size


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """Score for a single scoring dimension."""

    name: str
    raw_score: float
    weight: float
    weighted_score: float
    matched_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoringResult:
    """Complete scoring result for a request.

    Parameters
    ----------
    tier : ModelTier
        Recommended tier.
    score : float
        Raw weighted composite score.
    confidence : float
        Confidence in tier assignment (0-1), sigmoid-based.
    reason : str
        Why this tier was selected (scored, short_message, ambiguous, large_context).
    dimensions : tuple[DimensionScore, ...]
        Per-dimension breakdown.
    """

    tier: ModelTier
    score: float
    confidence: float
    reason: str
    dimensions: tuple[DimensionScore, ...] = ()


# ---------------------------------------------------------------------------
# Structural dimension scorers
# ---------------------------------------------------------------------------

_FILE_PATH_PATTERN = re.compile(r"(?:\b\w+/)+\w+\.\w+|\b\w+\.\w{1,4}\b")
_CONSTRAINT_PATTERN = re.compile(
    r"\b(must|must not|required|shall|shall not|ensure|always|never)\b", re.IGNORECASE
)
_FUNCTION_PATTERN = re.compile(r"\b\w+\(|\bclass \w+|\bdef \w+|\bfunction \w+")


def _score_prompt_length(text: str) -> float:
    """Score based on prompt length. Longer prompts = more complex."""
    length = len(text)
    if length < 50:
        return 0.0
    if length < 200:
        return 0.2
    if length < 500:
        return 0.4
    if length < 1000:
        return 0.6
    if length < 3000:
        return 0.8
    return 1.0


def _score_file_references(text: str) -> float:
    """Score based on file path references. More files = more complex."""
    matches = _FILE_PATH_PATTERN.findall(text)
    count = len(matches)
    if count == 0:
        return 0.0
    if count <= 2:
        return 0.3
    if count <= 5:
        return 0.6
    return 1.0


def _score_specificity(text: str) -> float:
    """Score based on specificity indicators (high specificity = simpler task).

    Direction is "down", so high scores here reduce overall complexity.
    """
    indicators = 0
    indicators += len(_FILE_PATH_PATTERN.findall(text))
    indicators += len(_FUNCTION_PATTERN.findall(text))
    if re.search(r"line \d+|:\d+", text):
        indicators += 2

    if indicators >= 5:
        return 1.0
    if indicators >= 3:
        return 0.7
    if indicators >= 1:
        return 0.4
    return 0.0


def _score_constraint_density(text: str) -> float:
    """Score based on constraint language density."""
    matches = _CONSTRAINT_PATTERN.findall(text)
    count = len(matches)
    if count == 0:
        return 0.0
    if count <= 2:
        return 0.3
    if count <= 5:
        return 0.6
    return 1.0


_STRUCTURAL_SCORERS: dict[str, Callable[[str], float]] = {
    "prompt_length": _score_prompt_length,
    "file_references": _score_file_references,
    "specificity": _score_specificity,
    "constraint_density": _score_constraint_density,
}


# ---------------------------------------------------------------------------
# Tier mapping (from Manifest's sigmoid.ts)
# ---------------------------------------------------------------------------


def _score_to_tier(score: float, boundaries: TierBoundaries) -> ModelTier:
    """Map raw score to tier using boundary thresholds."""
    if score < boundaries.simple_max:
        return ModelTier.FAST
    if score < boundaries.standard_max:
        return ModelTier.STANDARD
    if score < boundaries.complex_max:
        return ModelTier.COMPLEX
    return ModelTier.REASONING


def _compute_confidence(score: float, boundaries: TierBoundaries, k: float = 8.0) -> float:
    """Compute confidence as inverse sigmoid of distance to nearest boundary."""
    boundary_values = [boundaries.simple_max, boundaries.standard_max, boundaries.complex_max]
    min_distance = min(abs(score - b) for b in boundary_values)
    return 1.0 / (1.0 + math.exp(-k * min_distance))


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

_default_trie: KeywordTrie | None = None


def _get_default_trie() -> KeywordTrie:
    global _default_trie
    if _default_trie is None:
        keyword_dims = [d for d in SCORING_DIMENSIONS if d.keywords]
        _default_trie = KeywordTrie(keyword_dims)
    return _default_trie


def _score_keyword_dimension(
    dim_name: str,
    matches: list[tuple[str, str, int]],
    direction: str,
) -> tuple[float, tuple[str, ...]]:
    """Score a keyword dimension from trie matches.

    Returns
    -------
    tuple[float, tuple[str, ...]]
        (raw_score, matched_keywords)
    """
    dim_matches = [(kw, pos) for kw, dim, pos in matches if dim == dim_name]
    if not dim_matches:
        return 0.0, ()

    unique_keywords = tuple(sorted({kw for kw, _ in dim_matches}))
    count = len(dim_matches)

    raw = min(count / 3.0, 1.0)
    if direction == "down":
        raw = -raw

    return raw, unique_keywords


def score_request(
    prompt: str,
    boundaries: TierBoundaries = DEFAULT_BOUNDARIES,
) -> ScoringResult:
    """Score a request prompt for complexity and return a tier recommendation.

    Parameters
    ----------
    prompt : str
        The user's prompt text.
    boundaries : TierBoundaries
        Tier boundary thresholds.

    Returns
    -------
    ScoringResult
        Scoring result with tier, score, confidence, and dimension breakdown.
    """
    if not prompt or not prompt.strip():
        return ScoringResult(
            tier=ModelTier.STANDARD,
            score=0.0,
            confidence=0.4,
            reason="ambiguous",
        )

    text = prompt.strip()

    # Short message override (from Manifest's scorer)
    if len(text) < SHORT_MESSAGE_THRESHOLD:
        trie = _get_default_trie()
        short_matches = trie.scan(text)
        has_simple = any(dim == "simple_indicators" for _, dim, _ in short_matches)
        if has_simple:
            return ScoringResult(
                tier=ModelTier.FAST,
                score=-0.3,
                confidence=0.9,
                reason="short_message",
            )

    # Full scoring
    trie = _get_default_trie()
    all_matches = trie.scan(text)

    dimensions: list[DimensionScore] = []
    raw_score = 0.0

    for dim in SCORING_DIMENSIONS:
        if dim.keywords:
            dim_raw, matched_kws = _score_keyword_dimension(dim.name, all_matches, dim.direction)
            weighted = dim_raw * dim.weight
            dimensions.append(
                DimensionScore(
                    name=dim.name,
                    raw_score=dim_raw,
                    weight=dim.weight,
                    weighted_score=weighted,
                    matched_keywords=matched_kws,
                )
            )
        else:
            scorer_fn = _STRUCTURAL_SCORERS.get(dim.name)
            dim_raw = scorer_fn(text) if scorer_fn else 0.0
            if dim.direction == "down":
                dim_raw = -dim_raw
            weighted = dim_raw * dim.weight
            dimensions.append(
                DimensionScore(
                    name=dim.name,
                    raw_score=dim_raw,
                    weight=dim.weight,
                    weighted_score=weighted,
                )
            )

        raw_score += dimensions[-1].weighted_score

    # Long prompt floor (from Manifest's applyTierFloors)
    reason = "scored"
    tier = _score_to_tier(raw_score, boundaries)

    if len(text) > LONG_PROMPT_THRESHOLD:
        floor_tier = ModelTier.COMPLEX
        if _tier_order(tier) < _tier_order(floor_tier):
            tier = floor_tier
            reason = "large_context"

    confidence = _compute_confidence(raw_score, boundaries, CONFIDENCE_K)
    if confidence < CONFIDENCE_THRESHOLD and reason == "scored":
        tier = ModelTier.STANDARD
        reason = "ambiguous"

    return ScoringResult(
        tier=tier,
        score=raw_score,
        confidence=confidence,
        reason=reason,
        dimensions=tuple(dimensions),
    )


_TIER_ORDER: dict[ModelTier, int] = {
    ModelTier.FAST: 0,
    ModelTier.STANDARD: 1,
    ModelTier.COMPLEX: 2,
    ModelTier.REASONING: 3,
}


def _tier_order(tier: ModelTier) -> int:
    return _TIER_ORDER[tier]
