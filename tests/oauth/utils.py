from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
from http import HTTPStatus
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from django_mcpz.oauth import cimd
from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
)
from django_mcpz.tokens import sha256_hex

ISSUER = "http://testserver/oauth"
RESOURCE = "http://testserver/oauth-mcp"
REDIRECT_URI = "http://localhost:9999/callback"
METADATA_URL = "https://client.example/oauth/client.json"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def make_client(**fields: Any) -> Client:
    fields.setdefault("client_id", secrets.token_urlsafe(16))
    fields.setdefault("kind", Client.Kind.REGISTERED)
    fields.setdefault("name", "Test client")
    fields.setdefault("redirect_uris", [REDIRECT_URI])
    return Client.objects.create(**fields)


def make_access_token(
    *, user: User, client: Client | None = None, **fields: Any
) -> tuple[AccessToken, str]:
    if client is None:
        client = make_client()
    fields.setdefault("lifetime", dt.timedelta(hours=1))
    fields.setdefault("resource", RESOURCE)
    return AccessToken.create(client=client, user=user, **fields)


class TokenTestCase(TestCase):
    user: User
    oauth_client: Client

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")
        cls.oauth_client = make_client(name="Claude")

    def make_code(self, **fields: Any) -> tuple[AuthorizationCode, str, str]:
        verifier, challenge = pkce_pair()
        value = "mcp_code_" + secrets.token_urlsafe(16)
        fields.setdefault("client", self.oauth_client)
        fields.setdefault("user", self.user)
        fields.setdefault("redirect_uri", REDIRECT_URI)
        fields.setdefault("resource", RESOURCE)
        fields.setdefault("expires_at", timezone.now() + dt.timedelta(minutes=5))
        code = AuthorizationCode.objects.create(
            digest=sha256_hex(value), code_challenge=challenge, **fields
        )
        return code, value, verifier

    def post_token(self, **data: Any) -> Any:
        data.setdefault("client_id", self.oauth_client.client_id)
        return self.client.post("/oauth/token", data)

    def exchange(self, value: str, verifier: str, **data: Any) -> Any:
        data.setdefault("grant_type", "authorization_code")
        data.setdefault("code", value)
        data.setdefault("code_verifier", verifier)
        return self.post_token(**data)

    def assert_token_error(
        self,
        response: Any,
        error: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> dict[str, Any]:
        assert response.status_code == status
        assert response["Cache-Control"] == "no-store"
        body: dict[str, Any] = response.json()
        assert body["error"] == error
        return body

    def assert_tokens(self, response: Any) -> dict[str, Any]:
        assert response.status_code == HTTPStatus.OK
        assert response["Cache-Control"] == "no-store"
        body: dict[str, Any] = response.json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600
        access = AccessToken.objects.get(digest=sha256_hex(body["access_token"]))
        refresh = RefreshToken.objects.get(digest=sha256_hex(body["refresh_token"]))
        assert refresh.access_token == access
        assert access.is_valid
        assert refresh.is_valid
        return body


# From the RFC 5737 range set aside for documentation, for tests that mock
# resolution. Python classes these ranges as non-public, so the real check
# would refuse it, as tests of that check must bear in mind.
PUBLIC_ADDRESS = "203.0.113.10"


def mock_metadata_fetch(document: Any, *, body: bytes | None = None) -> Any:
    """Patch resolution and the fetch to return the document from a public host."""
    if body is None:
        body = json.dumps(document).encode()
    return mock.patch.multiple(
        cimd,
        resolve_public_address=mock.Mock(return_value=PUBLIC_ADDRESS),
        _fetch=mock.Mock(return_value=body),
    )
