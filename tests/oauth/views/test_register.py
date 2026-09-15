from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from django.test import TestCase, override_settings

from django_mcpz.oauth.models import Client
from tests.oauth.utils import REDIRECT_URI


class RegisterTests(TestCase):
    def register(self, metadata: Any) -> Any:
        return self.client.post(
            "/oauth/register",
            json.dumps(metadata),
            content_type="application/json",
        )

    def test_success(self):
        response = self.register(
            {
                "client_name": "Claude",
                "redirect_uris": [REDIRECT_URI, "https://claude.ai/callback"],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }
        )

        assert response.status_code == HTTPStatus.CREATED
        body = response.json()
        client = Client.objects.get()
        assert body["client_id"] == client.client_id
        assert body["client_name"] == "Claude"
        assert body["redirect_uris"] == [REDIRECT_URI, "https://claude.ai/callback"]
        assert body["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in body
        assert client.kind == Client.Kind.REGISTERED
        assert client.redirect_uris == [REDIRECT_URI, "https://claude.ai/callback"]

    def test_minimal(self):
        response = self.register({"redirect_uris": [REDIRECT_URI]})

        assert response.status_code == HTTPStatus.CREATED
        assert Client.objects.get().name == ""

    def test_invalid_json(self):
        response = self.client.post(
            "/oauth/register", "nope", content_type="application/json"
        )

        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json()["error"] == "invalid_client_metadata"

    def test_missing_redirect_uris(self):
        response = self.register({"client_name": "x"})

        assert response.json()["error"] == "invalid_redirect_uri"

    def test_insecure_redirect_uri(self):
        for uri in [
            "http://example.com/cb",
            "myapp://callback",
            "https://example.com/cb#frag",
            "http://[::1",
        ]:
            response = self.register({"redirect_uris": [uri]})

            assert response.json()["error"] == "invalid_redirect_uri", uri

    def test_non_string_redirect_uri(self):
        response = self.register({"redirect_uris": [1]})

        assert response.json()["error"] == "invalid_redirect_uri"

    def test_redirect_uri_too_long(self):
        response = self.register({"redirect_uris": ["https://x.example/" + "a" * 500]})

        assert response.json()["error"] == "invalid_redirect_uri"

    def test_bad_name(self):
        response = self.register({"redirect_uris": [REDIRECT_URI], "client_name": 1})

        assert response.json()["error"] == "invalid_client_metadata"

    def test_private_key_jwt(self):
        response = self.register(
            {
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "private_key_jwt",
            }
        )

        assert response.json()["error"] == "invalid_client_metadata"

    @override_settings(MCPZ_OAUTH_DYNAMIC_REGISTRATION=False)
    def test_disabled(self):
        response = self.register({"redirect_uris": [REDIRECT_URI]})

        assert response.status_code == HTTPStatus.NOT_FOUND
