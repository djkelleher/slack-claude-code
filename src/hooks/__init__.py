"""Hook system for event handling."""

from .registry import (
    HookRegistry as HookRegistry,
    create_context as create_context,
    hook as hook,
)
from .types import (
    HookContext as HookContext,
    HookEvent as HookEvent,
    HookEventType as HookEventType,
    HookResult as HookResult,
)
