"""
CLI commands for ccslack configuration management.

Usage:
    ccslack config set KEY=VALUE     Set a configuration value
    ccslack config get KEY           Get a configuration value
    ccslack config list              List all configuration keys
    ccslack config delete KEY        Delete a configuration value
    ccslack config path              Show config file path
"""

import sys

from src.config_storage import get_storage

# Keys that should be masked when displayed
SENSITIVE_KEYS = {
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
}


def mask_value(key: str, value: str) -> str:
    """Mask sensitive values for display."""
    if key in SENSITIVE_KEYS and value:
        if len(value) <= 8:
            return "****"
        return value[:4] + "*" * (len(value) - 8) + value[-4:]
    return value


def cmd_set(args: list[str]) -> int:
    """Set a configuration value."""
    if not args:
        print("Usage: ccslack config set KEY=VALUE")
        print("Example: ccslack config set SLACK_SIGNING_SECRET=abc123")
        return 1

    for arg in args:
        if "=" not in arg:
            print(f"Error: Invalid format '{arg}'. Use KEY=VALUE")
            return 1

        key, value = arg.split("=", 1)
        key = key.strip().upper()
        value = value.strip()

        if not key:
            print("Error: Key cannot be empty")
            return 1

        storage = get_storage()
        storage.set(key, value)
        print(f"Set {key}={mask_value(key, value)}")

    return 0


def cmd_get(args: list[str]) -> int:
    """Get a configuration value."""
    if not args:
        print("Usage: ccslack config get KEY")
        return 1

    storage = get_storage()
    key = args[0].strip().upper()
    value = storage.get(key)

    if value is None:
        print(f"{key}: (not set)")
    else:
        print(f"{key}={mask_value(key, str(value))}")

    return 0


def cmd_list(args: list[str]) -> int:
    """List all configuration keys."""
    storage = get_storage()
    data = storage.get_all()

    if not data:
        print("No configuration values set.")
        print(f"\nConfig file: {storage.config_file}")
        return 0

    print("Configuration values:")
    for key, value in sorted(data.items()):
        print(f"  {key}={mask_value(key, str(value))}")

    print(f"\nConfig file: {storage.config_file}")
    return 0


def cmd_delete(args: list[str]) -> int:
    """Delete a configuration value."""
    if not args:
        print("Usage: ccslack config delete KEY")
        return 1

    storage = get_storage()
    key = args[0].strip().upper()

    if storage.delete(key):
        print(f"Deleted {key}")
    else:
        print(f"{key}: not found")

    return 0


def cmd_path(args: list[str]) -> int:
    """Show config file path."""
    storage = get_storage()
    print(f"Config directory: {storage.config_dir}")
    print(f"Config file: {storage.config_file}")
    print(f"Database file: {storage.config_dir / 'slack_claude.db'}")
    return 0


def cmd_help(args: list[str]) -> int:
    """Show help."""
    print(__doc__)
    print("Commands:")
    print("  set KEY=VALUE    Set a configuration value (can set multiple)")
    print("  get KEY          Get a configuration value")
    print("  list             List all stored configuration values")
    print("  delete KEY       Delete a configuration value")
    print("  path             Show config file paths")
    print("")
    print("Common configuration keys:")
    print("  SLACK_BOT_TOKEN       Slack bot OAuth token (xoxb-...)")
    print("  SLACK_APP_TOKEN       Slack app-level token (xapp-...)")
    print("  SLACK_SIGNING_SECRET  Slack signing secret")
    print("  DEFAULT_WORKING_DIR   Default working directory for Claude")
    print("  DEFAULT_MODEL         Default Claude model (opus, sonnet, haiku)")
    return 0


COMMANDS = {
    "set": cmd_set,
    "get": cmd_get,
    "list": cmd_list,
    "delete": cmd_delete,
    "path": cmd_path,
    "help": cmd_help,
}


def main() -> int:
    """Main entry point for config CLI."""
    args = sys.argv[1:]

    # Handle: ccslack config <subcommand>
    # The entry point is "ccslack config" so args[0] is the subcommand
    if not args:
        return cmd_help([])

    subcommand = args[0].lower()
    subargs = args[1:]

    if subcommand in COMMANDS:
        return COMMANDS[subcommand](subargs)
    else:
        print(f"Unknown command: {subcommand}")
        print("Run 'ccslack config help' for usage.")
        return 1


def run():
    """Entry point wrapper."""
    sys.exit(main())


if __name__ == "__main__":
    run()
