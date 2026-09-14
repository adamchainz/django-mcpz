from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth.models import User
from django.test import TestCase

from django_mcpz import tokens
from django_mcpz.bearer_tokens.models import Token


class TokenAdminTests(TestCase):
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", password="pw")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_add_shows_value_once(self):
        response = self.client.post(
            "/admin/django_mcpz_bearer_tokens/token/add/",
            {"name": "Claude Code", "user": self.admin.pk},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        (message,) = [
            str(m) for m in response.context["messages"] if "shown only" in str(m)
        ]
        value = message.rsplit(" ", 1)[1]
        assert Token.objects.get().digest == tokens.sha256_hex(value)

    def test_change_keeps_digest(self):
        token, value = Token.create(name="Old name", user=self.admin)

        self.client.post(
            f"/admin/django_mcpz_bearer_tokens/token/{token.pk}/change/",
            {"name": "New name", "user": self.admin.pk},
        )

        token.refresh_from_db()
        assert token.name == "New name"
        assert token.digest == tokens.sha256_hex(value)

    def test_change_cannot_unrevoke(self):
        token, _ = Token.create(name="t", user=self.admin)
        token.revoke()

        response = self.client.post(
            f"/admin/django_mcpz_bearer_tokens/token/{token.pk}/change/",
            {"name": "t", "user": self.admin.pk, "revoked_at": ""},
        )

        assert response.status_code == HTTPStatus.FOUND
        token.refresh_from_db()
        assert token.is_revoked

    def test_revoke_action(self):
        token, _ = Token.create(name="t", user=self.admin)
        already, _ = Token.create(name="already", user=self.admin)
        already.revoke()

        response = self.client.post(
            "/admin/django_mcpz_bearer_tokens/token/",
            {"action": "revoke", "_selected_action": [token.pk, already.pk]},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        token.refresh_from_db()
        assert token.is_revoked
        (message,) = list(response.context["messages"])
        assert str(message) == "Revoked 1 token(s)."
