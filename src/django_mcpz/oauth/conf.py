from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.test.signals import setting_changed
from django.utils.functional import cached_property

# Compared against urlsplit().hostname, which strips the brackets from IPv6
# literals, so the loopback address appears without them.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OAuthSettings:
    def __init__(self) -> None:
        setting_changed.connect(self.on_setting_changed)

    def on_setting_changed(self, sender: Any, setting: str, **kwargs: Any) -> None:
        match setting:
            case "MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME":
                self.__dict__.pop("access_token_lifetime", None)
            case "MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME":
                self.__dict__.pop("refresh_token_lifetime", None)
            case "MCPZ_OAUTH_DYNAMIC_REGISTRATION":
                self.__dict__.pop("dynamic_registration", None)
            case _:
                return

    @cached_property
    def access_token_lifetime(self) -> dt.timedelta:
        return getattr(
            settings, "MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME", dt.timedelta(hours=1)
        )

    @cached_property
    def refresh_token_lifetime(self) -> dt.timedelta:
        return getattr(
            settings, "MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME", dt.timedelta(days=30)
        )

    @cached_property
    def code_lifetime(self) -> dt.timedelta:
        return dt.timedelta(minutes=5)

    @cached_property
    def dynamic_registration(self) -> bool:
        return getattr(settings, "MCPZ_OAUTH_DYNAMIC_REGISTRATION", True)


oauth_settings = OAuthSettings()


def is_secure_url(url: str) -> bool:
    """Whether the URL is HTTPS, or plain HTTP to a local host."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
