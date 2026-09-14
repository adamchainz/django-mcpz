from __future__ import annotations

from http import HTTPStatus
from typing import Any
from unittest import mock

from django_mcpz.oauth import cimd
from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
)
from tests.oauth.utils import METADATA_URL, TokenTestCase, make_client


class RevokeTests(TokenTestCase):
    def issue(self) -> dict[str, Any]:
        _, value, verifier = self.make_code()
        return self.assert_tokens(self.exchange(value, verifier))

    def revoke(self, token: str, **data: Any) -> Any:
        data.setdefault("client_id", self.oauth_client.client_id)
        return self.client.post("/oauth/revoke", {"token": token, **data})

    def test_refresh_token(self):
        tokens = self.issue()

        response = self.revoke(tokens["refresh_token"])

        assert response.status_code == HTTPStatus.OK
        assert not AccessToken.objects.get().is_valid
        assert not RefreshToken.objects.get().is_valid

    def test_refresh_token_revokes_family(self):
        first = self.issue()
        second = self.assert_tokens(
            self.post_token(
                grant_type="refresh_token", refresh_token=first["refresh_token"]
            )
        )

        self.revoke(second["refresh_token"])

        assert not any(token.is_valid for token in AccessToken.objects.all())
        assert not any(token.is_valid for token in RefreshToken.objects.all())

    def test_refresh_token_without_code(self):
        tokens = self.issue()
        AuthorizationCode.objects.all().delete()

        self.revoke(tokens["refresh_token"])

        assert not AccessToken.objects.get().is_valid
        assert not RefreshToken.objects.get().is_valid

    def test_access_token(self):
        tokens = self.issue()

        response = self.revoke(tokens["access_token"], token_type_hint="access_token")

        assert response.status_code == HTTPStatus.OK
        assert not AccessToken.objects.get().is_valid
        assert RefreshToken.objects.get().is_valid

    def test_unknown_token(self):
        response = self.revoke("mcp_nope")

        assert response.status_code == HTTPStatus.OK

    def test_other_client(self):
        tokens = self.issue()
        other = make_client()

        self.revoke(tokens["refresh_token"], client_id=other.client_id)
        self.revoke(tokens["access_token"], client_id=other.client_id)

        assert AccessToken.objects.get().is_valid
        assert RefreshToken.objects.get().is_valid

    def test_unknown_client(self):
        response = self.revoke("mcp_nope", client_id="nope")

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_metadata_client_unknown_not_fetched(self):
        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.revoke("mcp_nope", client_id=METADATA_URL)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert not fetch.called
        assert not Client.objects.filter(client_id=METADATA_URL).exists()
