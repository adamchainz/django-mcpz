from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase, override_settings

from django_mcpz.oauth.conf import is_secure_url, oauth_settings


class SettingsTests(SimpleTestCase):
    def test_defaults(self):
        assert oauth_settings.access_token_lifetime == dt.timedelta(hours=1)
        assert oauth_settings.refresh_token_lifetime == dt.timedelta(days=30)
        assert oauth_settings.code_lifetime == dt.timedelta(minutes=5)
        assert oauth_settings.dynamic_registration

    @override_settings(
        MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME=dt.timedelta(minutes=10),
        MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME=dt.timedelta(days=1),
        MCPZ_OAUTH_DYNAMIC_REGISTRATION=False,
    )
    def test_overrides(self):
        assert oauth_settings.access_token_lifetime == dt.timedelta(minutes=10)
        assert oauth_settings.refresh_token_lifetime == dt.timedelta(days=1)
        assert not oauth_settings.dynamic_registration

    def test_is_secure_url(self):
        assert is_secure_url("https://example.com/cb")
        assert is_secure_url("http://localhost:3000/cb")
        assert is_secure_url("http://127.0.0.1:3000/cb")
        assert is_secure_url("http://[::1]:3000/cb")
        assert not is_secure_url("http://example.com/cb")
        assert not is_secure_url("myapp://callback")
