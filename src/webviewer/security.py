"""Security utilities for signed URLs."""

import hashlib
import hmac
import time
from typing import Optional

from src.config import config


def generate_signed_url(content_id: str, expires_in: Optional[int] = None) -> str:
    """Generate a signed URL for viewing content.

    Parameters
    ----------
    content_id : str
        The content ID to include in the URL.
    expires_in : Optional[int]
        Seconds until the URL expires. Defaults to config TTL.

    Returns
    -------
    str
        The signed URL.
    """
    if expires_in is None:
        expires_in = config.timeouts.webviewer.content_ttl

    expires = int(time.time()) + expires_in
    signature = _create_signature(content_id, expires)
    base_url = config.timeouts.webviewer.base_url.rstrip("/")

    return f"{base_url}/view/{content_id}?expires={expires}&sig={signature}"


def validate_signature(content_id: str, expires: int, signature: str) -> bool:
    """Validate a URL signature.

    Parameters
    ----------
    content_id : str
        The content ID from the URL.
    expires : int
        The expiration timestamp from the URL.
    signature : str
        The signature from the URL.

    Returns
    -------
    bool
        True if the signature is valid and not expired.
    """
    # Check expiration
    if time.time() > expires:
        return False

    # Validate signature
    expected = _create_signature(content_id, expires)
    return hmac.compare_digest(signature, expected)


def _create_signature(content_id: str, expires: int) -> str:
    """Create HMAC signature for content_id and expiration.

    Parameters
    ----------
    content_id : str
        The content ID.
    expires : int
        The expiration timestamp.

    Returns
    -------
    str
        The hex-encoded HMAC signature.
    """
    secret = config.timeouts.webviewer.secret_key
    payload = f"{content_id}:{expires}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
