# Claude Code Guidelines

## Import Management

- Don't ever use optional dependency management
- Always assume all modules are installed and available
- Don't use ImportError catch blocks
- Don't use lazy imports
- All imports should go at the top of the file

## Environment

- Python: `/home/dan/miniforge3/envs/trading/bin/python`
- postgres / timescale: postgresql://danklab777:danklab987123@0.0.0.0:5653/danklab

## General
- try not to use getattr. please check what attributes objects have and use object.attribute to access it.
- Do not use hasattr. We need to check what attributes objects have when the code is written.
- Do not every revert files back to older git commits without asking first.
- Do not create nested folders with only a single file in it.
- Commit all changes after code is modified. use a detailed and clear commit message.

## Import Management
- don't make __all__ definitions anywhere
- All imports at top of files - no lazy imports, no `try/except ImportError` blocks
- Never use ImportError handling - assume all imports succeed
- Don't implement things for backward compatability (do not re-export, do not use name aliases, do not make wrapper functions)
- Use canonical module locations: `from quant.core import Greeks` (not from submodules)
- When refactoring moves a module, update all imports directly - don't re-export from old location for backward compatibility
- Order: stdlib → third-party → local (isort with black profile)
- Line length: 100 characters max

## Code Style

- Formatter: black (100 char lines)
- Import sorting: isort (black profile)
- Linting: flake8 (max complexity 10)
- Type hints required on all function signatures
- Docstrings: NumPy convention

## Naming Conventions

- Classes: `PascalCase` (OrderStatus, BrokerAdapter)
- Functions/methods: `snake_case` (place_order, get_positions)
- Constants: `UPPER_SNAKE_CASE` (TERMINAL_ORDER_STATUSES)
- Private: leading underscore (_request, _build_payload)
- Enum values: `lowercase` string values (OrderStatus.PENDING = "pending")
