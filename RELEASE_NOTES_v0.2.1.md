# Release v0.2.1

**Release Date:** 2026-02-02

## Summary

Patch release to fix a missing dependency that caused test failures in CI environments.

## Bug Fixes

- **Add missing PyYAML dependency** - The `src/agents/registry.py` module imports `yaml` but PyYAML was not listed in `pyproject.toml`, causing test collection to fail with `ModuleNotFoundError: No module named 'yaml'`

## Installation

```bash
pip install slack-claude-code==0.2.1
```

Or with Poetry:
```bash
poetry add slack-claude-code@0.2.1
```

## Full Changelog

https://github.com/your-org/slack-claude-code/compare/v0.2.0...v0.2.1
