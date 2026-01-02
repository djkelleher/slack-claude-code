"""Pytest fixtures for Slack Claude Code tests."""

import asyncio
import pytest

from src.hooks import HookRegistry


@pytest.fixture(autouse=True)
def clean_hook_registry():
    """Clear hook registry before and after each test."""
    HookRegistry.clear()
    yield
    HookRegistry.clear()


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
