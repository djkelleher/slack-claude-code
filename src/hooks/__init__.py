"""Hook system for event handling."""

from .registry import HookRegistry as HookRegistry
from .registry import create_context as create_context
from .registry import hook as hook
from .types import HookContext as HookContext
from .types import HookEvent as HookEvent
from .types import HookEventType as HookEventType
from .types import HookResult as HookResult
