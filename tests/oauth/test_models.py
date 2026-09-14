from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from django_mcpz.oauth.models import AuthorizationCode
from tests.oauth.utils import make_access_token, make_client


class ModelTests(TestCase):
    def test_revoke_twice_keeps_first_time(self):
        user = User.objects.create_user("alice")
        token, _ = make_access_token(user=user)
        token.revoke()
        first = token.revoked_at

        token.revoke()

        assert token.revoked_at == first

    def test_str(self):
        user = User.objects.create_user("alice")
        client = make_client(name="")
        token, _ = make_access_token(user=user, client=client)
        code = AuthorizationCode.objects.create(
            digest="a",
            client=client,
            user=user,
            redirect_uri="",
            code_challenge="",
            resource="",
            expires_at=timezone.now(),
        )

        assert str(client) == client.client_id
        assert str(token) == f"{client.client_id} as alice"
        assert str(code) == f"Code for {client.client_id} by alice"
