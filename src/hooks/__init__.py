"""Hook system for event handling."""

from .registry import HookRegistry, create_context, hook
from .types import HookContext, HookEvent, HookEventType, HookResult
