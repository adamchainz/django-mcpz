from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings

# Compared against urlsplit().hostname, which strips the brackets from IPv6
# literals, so the loopback address appears without them.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class OAuthSettings:
    access_token_lifetime: dt.timedelta
    refresh_token_lifetime: dt.timedelta
    code_lifetime: dt.timedelta
    dynamic_registration: bool


def get_settings() -> OAuthSettings:
    """
    Read the app's settings, all optional.

    Read per use rather than at import time, so that override_settings works
    in tests.
    """
    return OAuthSettings(
        access_token_lifetime=getattr(
            settings, "MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME", dt.timedelta(hours=1)
        ),
        refresh_token_lifetime=getattr(
            settings, "MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME", dt.timedelta(days=30)
        ),
        code_lifetime=dt.timedelta(minutes=5),
        dynamic_registration=getattr(settings, "MCPZ_OAUTH_DYNAMIC_REGISTRATION", True),
    )


def is_secure_url(url: str) -> bool:
    """Whether the URL is HTTPS, or plain HTTP to a local host."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
