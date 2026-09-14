"""
Token values, shared by the bearer_tokens and oauth apps.
"""

from __future__ import annotations

import hashlib
import secrets

# A generic prefix, so leaked tokens are recognisable by secret scanners
# without naming the server software.
PREFIX = "mcp_"


def generate() -> str:
    """Generate a new token value, to be digested before storage."""
    return PREFIX + secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
