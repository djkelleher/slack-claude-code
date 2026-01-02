#!/usr/bin/env python3
"""Convenience script to run the Slack Claude Code bot."""

import asyncio
from src.app import main

if __name__ == "__main__":
    asyncio.run(main())
