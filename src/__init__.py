# Slack Claude Code Bot

import asyncio

from src.app import main as _main


def main():
    """Entry point for the ccslack CLI command."""
    asyncio.run(_main())
