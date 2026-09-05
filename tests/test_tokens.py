from __future__ import annotations

import datetime as dt
from http import HTTPStatus
from io import StringIO

import pytest
from django.contrib.auth.models import Permission, User
from django.core.management import CommandError, call_command
from django.http import HttpRequest
from django.test import RequestFactory, TestCase
from django.utils import timezone

from django_mcpz.jsonrpc import INVALID_PARAMS
from django_mcpz.tokens.auth import token_auth
from django_mcpz.tokens.models import TOKEN_PREFIX, Token
from tests.test_server import ServerTestCase, make_message


class TokenModelTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def test_create(self):
        token, value = Token.create(name="Alice’s laptop", user=self.user)

        assert value.startswith(TOKEN_PREFIX)
        assert token.digest == Token.digest_of(value)
        assert token.user == self.user
        assert list(self.user.mcp_tokens.all()) == [token]
        assert not token.is_revoked
        assert str(token) == "Alice’s laptop"

    def test_revoke(self):
        token, _ = Token.create(name="Old", user=self.user)

        token.revoke()

        token.refresh_from_db()
        assert token.is_revoked

    def test_values_unique(self):
        assert Token.generate() != Token.generate()

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


class TokenAuthTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def request(self, authorization: str | None) -> HttpRequest:
        headers = {} if authorization is None else {"Authorization": authorization}
        return RequestFactory().post("/mcp", headers=headers)

    def test_missing_header(self):
        response = token_auth(self.request(None))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_wrong_scheme(self):
        _, value = Token.create(name="t", user=self.user)

        response = token_auth(self.request(f"Basic {value}"))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_empty_credential(self):
        response = token_auth(self.request("Bearer "))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_unknown_token(self):
        response = token_auth(self.request("Bearer mcp_nope"))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_revoked_token(self):
        token, value = Token.create(name="t", user=self.user)
        token.revoke()

        response = token_auth(self.request(f"Bearer {value}"))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_expired_token(self):
        _, value = Token.create(
            name="t",
            user=self.user,
            expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        response = token_auth(self.request(f"Bearer {value}"))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_unexpired_token(self):
        _, value = Token.create(
            name="t",
            user=self.user,
            expires_at=timezone.now() + dt.timedelta(days=1),
        )

        assert token_auth(self.request(f"Bearer {value}")) is None

    def test_inactive_user(self):
        _, value = Token.create(name="t", user=self.user)
        self.user.is_active = False
        self.user.save()

        response = token_auth(self.request(f"Bearer {value}"))

        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_valid_token(self):
        token, value = Token.create(name="t", user=self.user)
        # The auth scheme is case-insensitive (RFC 9110 §11.1).
        request = self.request(f"bearer {value}")

        assert token_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]
        assert request.user == self.user
        token.refresh_from_db()
        assert token.last_used_at is not None


class TokensServerTests(ServerTestCase, TestCase):
    """The tokens app authenticating a server, end to end."""

    url = "/tokens-mcp"
    viewer: User
    nobody: User

    @classmethod
    def setUpTestData(cls):
        cls.viewer = User.objects.create_user("viewer")
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="tests", codename="view_widget"
            )
        )
        cls.nobody = User.objects.create_user("nobody")

    def test_unauthenticated(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_whoami(self):
        _, value = Token.create(name="Viewer’s client", user=self.viewer)

        response = self.post(
            make_message("tools/call", {"name": "whoami", "arguments": {}}),
            headers={"Authorization": f"Bearer {value}"},
        )

        assert response.json()["result"]["structuredContent"] == {
            "token": "Viewer’s client",
            "user": "viewer",
        }

    def test_permission_granted(self):
        _, value = Token.create(name="t", user=self.viewer)

        response = self.post(
            make_message("tools/list"), headers={"Authorization": f"Bearer {value}"}
        )
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        assert names == ["whoami", "token_widget_tool"]

        response = self.post(
            make_message("tools/call", {"name": "token_widget_tool"}),
            headers={"Authorization": f"Bearer {value}"},
        )
        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "widgets"}]

    def test_permission_denied(self):
        _, value = Token.create(name="t", user=self.nobody)

        response = self.post(
            make_message("tools/list"), headers={"Authorization": f"Bearer {value}"}
        )
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        assert names == ["whoami"]

        response = self.post(
            make_message("tools/call", {"name": "token_widget_tool"}),
            headers={"Authorization": f"Bearer {value}"},
        )
        self.assert_error(response, INVALID_PARAMS)


class CreateMCPTokenCommandTests(TestCase):
    def test_create(self):
        user = User.objects.create_user("alice")
        out = StringIO()

        call_command("create_mcp_token", "Laptop", user="alice", stdout=out)

        value = out.getvalue().strip()
        assert value.startswith(TOKEN_PREFIX)
        token = Token.objects.get()
        assert token.name == "Laptop"
        assert token.user == user
        assert token.digest == Token.digest_of(value)

    def test_expires_in_days(self):
        User.objects.create_user("alice")

        call_command(
            "create_mcp_token",
            "Laptop",
            user="alice",
            expires_in_days=30,
            stdout=StringIO(),
        )

        token = Token.objects.get()
        assert token.expires_at is not None
        remaining = token.expires_at - timezone.now()
        assert dt.timedelta(days=29, hours=23) < remaining <= dt.timedelta(days=30)

    def test_expires_in_days_not_positive(self):
        User.objects.create_user("alice")

        with pytest.raises(CommandError, match="must be positive"):
            call_command("create_mcp_token", "Laptop", user="alice", expires_in_days=0)

    def test_user_required(self):
        with pytest.raises(CommandError, match="--user"):
            call_command("create_mcp_token", "Laptop")

    def test_unknown_user(self):
        with pytest.raises(CommandError, match="No user named 'nobody'"):
            call_command("create_mcp_token", "Laptop", user="nobody")


class TokenAdminTests(TestCase):
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", password="pw")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_add_shows_value_once(self):
        response = self.client.post(
            "/admin/django_mcpz_tokens/token/add/",
            {"name": "Claude Code", "user": self.admin.pk},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        (message,) = [
            str(m) for m in response.context["messages"] if "shown only" in str(m)
        ]
        value = message.rsplit(" ", 1)[1]
        assert Token.objects.get().digest == Token.digest_of(value)

    def test_change_keeps_digest(self):
        token, value = Token.create(name="Old name", user=self.admin)

        self.client.post(
            f"/admin/django_mcpz_tokens/token/{token.pk}/change/",
            {"name": "New name", "user": self.admin.pk},
        )

        token.refresh_from_db()
        assert token.name == "New name"
        assert token.digest == Token.digest_of(value)

    def test_change_cannot_unrevoke(self):
        token, _ = Token.create(name="t", user=self.admin)
        token.revoke()

        response = self.client.post(
            f"/admin/django_mcpz_tokens/token/{token.pk}/change/",
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
            "/admin/django_mcpz_tokens/token/",
            {"action": "revoke", "_selected_action": [token.pk, already.pk]},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        token.refresh_from_db()
        assert token.is_revoked
        (message,) = list(response.context["messages"])
        assert str(message) == "Revoked 1 token(s)."
