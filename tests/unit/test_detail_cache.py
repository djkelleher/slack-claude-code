"""Unit tests for DetailCache."""

from collections import OrderedDict

import pytest

from src.utils.detail_cache import CachedDetail, DetailCache


@pytest.fixture(autouse=True)
def clear_detail_cache():
    """Reset cache state before and after each test."""
    original_max_age = DetailCache._max_age_seconds
    original_max_entries = DetailCache._max_entries
    DetailCache.clear()
    yield
    DetailCache.clear()
    DetailCache._max_age_seconds = original_max_age
    DetailCache._max_entries = original_max_entries


def test_detail_cache_store_get_and_refresh_lru(monkeypatch) -> None:
    """Fetching an entry should return content and move it to the LRU tail."""
    current_time = 1000.0
    monkeypatch.setattr("src.utils.detail_cache.time.time", lambda: current_time)

    DetailCache.store(1, "first")
    current_time += 1
    DetailCache.store(2, "second")

    assert DetailCache.get(1) == "first"
    assert list(DetailCache._cache.keys()) == [2, 1]


def test_detail_cache_expires_entries(monkeypatch) -> None:
    """Expired entries should be removed on access."""
    current_time = 1000.0
    monkeypatch.setattr("src.utils.detail_cache.time.time", lambda: current_time)
    DetailCache._max_age_seconds = 10

    DetailCache.store(1, "first")
    current_time += 11

    assert DetailCache.get(1) is None
    assert 1 not in DetailCache._cache


def test_detail_cache_cleanup_enforces_max_entries(monkeypatch) -> None:
    """Cleanup should evict the least recently used entries when size is exceeded."""
    current_time = 1000.0
    monkeypatch.setattr("src.utils.detail_cache.time.time", lambda: current_time)
    DetailCache._max_entries = 2

    DetailCache.store(1, "first")
    current_time += 1
    DetailCache.store(2, "second")
    current_time += 1
    DetailCache.store(3, "third")

    assert list(DetailCache._cache.keys()) == [2, 3]
    assert DetailCache.get(1) is None


def test_detail_cache_store_removes_expired_entries_during_cleanup(monkeypatch) -> None:
    """Store-time cleanup should drop expired entries before enforcing size."""
    current_time = 1000.0
    monkeypatch.setattr("src.utils.detail_cache.time.time", lambda: current_time)
    DetailCache._max_age_seconds = 10
    DetailCache._cache[1] = CachedDetail(command_id=1, content="old", created_at=980.0)

    DetailCache.store(2, "fresh")

    assert list(DetailCache._cache.keys()) == [2]


def test_detail_cache_clear_empties_cache() -> None:
    """Clear should remove all cached details."""
    DetailCache._cache = OrderedDict(
        {
            1: CachedDetail(command_id=1, content="a", created_at=1.0),
            2: CachedDetail(command_id=2, content="b", created_at=2.0),
        }
    )

    DetailCache.clear()

    assert DetailCache._cache == OrderedDict()
