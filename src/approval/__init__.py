"""Permission approval handling via Slack."""

from .handler import PendingApproval, PermissionManager
from .slack_ui import build_approval_blocks, build_approval_result_blocks
