from __future__ import annotations

import datetime as dt

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from django_mcpz import tokens
from django_mcpz.bearer_tokens.models import Token


class TokenModelTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def test_create(self):
        token, value = Token.create(name="Alice’s laptop", user=self.user)

        assert value.startswith(tokens.PREFIX)
        assert token.digest == Token.digest_of(value)
        assert token.user == self.user
        assert list(self.user.mcp_bearer_tokens.all()) == [token]
        assert not token.is_revoked
        assert str(token) == "Alice’s laptop"

    def test_revoke(self):
        token, _ = Token.create(name="Old", user=self.user)

        token.revoke()

        token.refresh_from_db()
        assert token.is_revoked

    def test_expiry(self):
        token, _ = Token.create(name="t", user=self.user)
        assert not token.is_expired

        token.expires_at = timezone.now() + dt.timedelta(days=1)
        assert not token.is_expired

        token.expires_at = timezone.now() - dt.timedelta(seconds=1)
        assert token.is_expired

    def test_record_use(self):
        token, _ = Token.create(name="t", user=self.user)
        assert token.last_used_at is None

        token.record_use()

        reloaded = Token.objects.get(pk=token.pk)
        assert reloaded.last_used_at is not None
        assert token.last_used_at == reloaded.last_used_at

    def test_record_use_throttled(self):
        token, _ = Token.create(name="t", user=self.user)
        token.record_use()
        first = token.last_used_at

        with self.assertNumQueries(0):
            token.record_use()

        assert token.last_used_at == first

    def test_record_use_after_resolution(self):
        token, _ = Token.create(name="t", user=self.user)
        token.last_used_at = timezone.now() - Token.LAST_USED_RESOLUTION
        token.save()
        first = token.last_used_at

        token.record_use()

        assert token.last_used_at is not None
        assert token.last_used_at > first
