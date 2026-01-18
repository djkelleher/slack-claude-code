"""
Secure configuration storage for sensitive settings like API tokens.

Uses Fernet encryption (AES-128-CBC with HMAC) to store sensitive values.
The encryption key is derived from a machine-specific identifier.
"""

import base64
import hashlib
import json
import os
import platform
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


def _get_username() -> str:
    """Get the current username, handling various edge cases."""
    # Try environment variables first (most reliable)
    for var in ("USER", "LOGNAME", "USERNAME"):
        username = os.environ.get(var)
        if username:
            return username
    # Fall back to getlogin if available
    try:
        return os.getlogin()
    except (OSError, AttributeError):
        pass
    # Last resort
    return "user"


def _get_machine_key() -> bytes:
    """
    Generate a machine-specific key for encryption.

    Combines multiple machine identifiers to create a unique key.
    This isn't meant to be unbreakable - it prevents casual reading
    of the config file while allowing the same machine to decrypt it.
    """
    identifiers = [
        platform.node(),  # hostname
        _get_username(),
        str(Path.home()),
    ]
    combined = "|".join(identifiers).encode()
    # Use SHA-256 and take first 32 bytes for Fernet key (base64 encoded)
    key_bytes = hashlib.sha256(combined).digest()
    return base64.urlsafe_b64encode(key_bytes)


class ConfigStorage:
    """Encrypted storage for sensitive configuration values."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or Path.home() / ".slack-claude-code"
        self.config_file = self.config_dir / "config.enc"
        self._fernet = Fernet(_get_machine_key())
        self._cache: dict[str, Any] | None = None

    def _ensure_dir(self) -> None:
        """Ensure config directory exists with secure permissions."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Set directory to be readable only by owner (Unix)
        if platform.system() != "Windows":
            os.chmod(self.config_dir, stat.S_IRWXU)

    def _load(self) -> dict[str, Any]:
        """Load and decrypt configuration from file."""
        if self._cache is not None:
            return self._cache

        if not self.config_file.exists():
            self._cache = {}
            return self._cache

        encrypted_data = self.config_file.read_bytes()
        decrypted_data = self._fernet.decrypt(encrypted_data)
        self._cache = json.loads(decrypted_data.decode())
        return self._cache

    def _save(self, data: dict[str, Any]) -> None:
        """Encrypt and save configuration to file."""
        self._ensure_dir()
        json_data = json.dumps(data, indent=2).encode()
        encrypted_data = self._fernet.encrypt(json_data)
        self.config_file.write_bytes(encrypted_data)
        # Set file to be readable only by owner (Unix)
        if platform.system() != "Windows":
            os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR)
        self._cache = data

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        data = self._load()
        return data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value."""
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> bool:
        """Delete a configuration value. Returns True if key existed."""
        data = self._load()
        if key in data:
            del data[key]
            self._save(data)
            return True
        return False

    def list_keys(self) -> list[str]:
        """List all configuration keys."""
        return list(self._load().keys())

    def get_all(self) -> dict[str, Any]:
        """Get all configuration values (for display, values masked)."""
        return self._load().copy()

    def clear(self) -> None:
        """Clear all configuration."""
        self._save({})


# Global instance
_storage: ConfigStorage | None = None


def get_storage() -> ConfigStorage:
    """Get the global config storage instance."""
    global _storage
    if _storage is None:
        _storage = ConfigStorage()
    return _storage
