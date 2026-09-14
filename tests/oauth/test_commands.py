from __future__ import annotations

import datetime as dt
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, modify_settings
from django.utils import timezone

from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
)
from tests.oauth.utils import make_access_token, make_client


class ClearCommandTests(TestCase):
    def test_not_installed(self):
        with (
            modify_settings(INSTALLED_APPS={"remove": "django_mcpz.oauth"}),
            pytest.raises(CommandError, match="invalid choice: 'oauth'"),
        ):
            call_command("mcpz", "oauth", "clear")

    def test_clears(self):
        user = User.objects.create_user("alice")
        now = timezone.now()
        stale_client = make_client(created_at=now - dt.timedelta(days=2))
        AuthorizationCode.objects.create(
            digest="stale-client-code",
            client=stale_client,
            user=user,
            redirect_uri="",
            code_challenge="",
            resource="",
            expires_at=now + dt.timedelta(minutes=1),
        )
        fresh_client = make_client(created_at=now - dt.timedelta(hours=2))
        used_client = make_client(
            created_at=now - dt.timedelta(days=2), last_used_at=now
        )
        AuthorizationCode.objects.create(
            digest="a",
            client=used_client,
            user=user,
            redirect_uri="",
            code_challenge="",
            resource="",
            expires_at=now - dt.timedelta(minutes=1),
        )
        live_code = AuthorizationCode.objects.create(
            digest="b",
            client=used_client,
            user=user,
            redirect_uri="",
            code_challenge="",
            resource="",
            expires_at=now + dt.timedelta(minutes=1),
        )
        # Expired and exchanged, but its tokens still link to it.
        used_code = AuthorizationCode.objects.create(
            digest="c",
            client=used_client,
            user=user,
            redirect_uri="",
            code_challenge="",
            resource="",
            expires_at=now - dt.timedelta(minutes=1),
            used_at=now - dt.timedelta(minutes=2),
        )
        expired, _ = make_access_token(
            user=user, client=used_client, lifetime=dt.timedelta(-1)
        )
        revoked, _ = make_access_token(user=user, client=used_client)
        revoked.revoke()
        live, _ = make_access_token(user=user, client=used_client)
        RefreshToken.create(
            lifetime=dt.timedelta(-1),
            client=used_client,
            user=user,
            resource="",
            access_token=live,
        )
        # An expired access token whose refresh token is still live: the
        # client will use the refresh token to get a new pair.
        refreshable, _ = make_access_token(
            user=user, client=used_client, lifetime=dt.timedelta(-1), code=used_code
        )
        live_refresh, _ = RefreshToken.create(
            lifetime=dt.timedelta(days=1),
            client=used_client,
            user=user,
            resource="",
            code=used_code,
            access_token=refreshable,
        )
        out = StringIO()

        call_command("mcpz", "oauth", "clear", stdout=out)

        assert out.getvalue() == (
            "Deleted 1 refresh tokens.\nDeleted 2 access tokens.\nDeleted 1 codes.\n"
            "Deleted 1 clients.\n"
        )
        assert set(Client.objects.all()) == {fresh_client, used_client}
        assert set(AuthorizationCode.objects.all()) == {live_code, used_code}
        assert set(AccessToken.objects.all()) == {live, refreshable}
        assert list(RefreshToken.objects.all()) == [live_refresh]
