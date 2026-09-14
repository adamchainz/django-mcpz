from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import http.server
import json
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http import HTTPStatus
from io import StringIO
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.http import HttpRequest, HttpResponse
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    modify_settings,
    override_settings,
)
from django.utils import timezone

from django_mcpz.oauth import cimd, conf, discovery, urls, wellknown
from django_mcpz.oauth.auth import oauth_auth
from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
    digest_of,
)
from tests.test_server import ServerTestCase, make_message

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


# -- Settings


class SettingsTests(SimpleTestCase):
    def test_defaults(self):
        conf_settings = conf.get_settings()

        assert conf_settings.access_token_lifetime == dt.timedelta(hours=1)
        assert conf_settings.refresh_token_lifetime == dt.timedelta(days=30)
        assert conf_settings.code_lifetime == dt.timedelta(minutes=5)
        assert conf_settings.dynamic_registration

    @override_settings(
        MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME=dt.timedelta(minutes=10),
        MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME=dt.timedelta(days=1),
        MCPZ_OAUTH_DYNAMIC_REGISTRATION=False,
    )
    def test_overrides(self):
        conf_settings = conf.get_settings()

        assert conf_settings.access_token_lifetime == dt.timedelta(minutes=10)
        assert conf_settings.refresh_token_lifetime == dt.timedelta(days=1)
        assert not conf_settings.dynamic_registration

    def test_is_secure_url(self):
        assert conf.is_secure_url("https://example.com/cb")
        assert conf.is_secure_url("http://localhost:3000/cb")
        assert conf.is_secure_url("http://127.0.0.1:3000/cb")
        assert conf.is_secure_url("http://[::1]:3000/cb")
        assert not conf.is_secure_url("http://example.com/cb")
        assert not conf.is_secure_url("myapp://callback")


class DiscoveryTests(SimpleTestCase):
    def test_canonical(self):
        assert discovery.canonical("HTTPS://Example.COM/MCP?x=1") == (
            "https://example.com/MCP?x=1"
        )

    def test_issuer_path(self):
        assert discovery.issuer_path() == "/oauth"

    def test_issuer_for(self):
        request = RequestFactory().get("/oauth/authorize", SERVER_NAME="TESTSERVER")

        assert discovery.issuer_for(request) == ISSUER

    def test_mcp_server_at(self):
        from tests.example import oauth_server, server

        assert discovery.mcp_server_at("/oauth-mcp") is oauth_server
        assert discovery.mcp_server_at("/mcp") is server
        assert discovery.mcp_server_at("/oauth/authorize") is None
        assert discovery.mcp_server_at("/admin/") is None
        assert discovery.mcp_server_at("/nope") is None
        assert discovery.mcp_server_at("/") is None

    def test_validate_resource(self):
        assert discovery.validate_resource(RESOURCE) == RESOURCE
        assert discovery.validate_resource("HTTP://TESTSERVER/oauth-mcp") == RESOURCE
        assert discovery.validate_resource("https://testserver/oauth-mcp") == (
            "https://testserver/oauth-mcp"
        )

    def test_validate_resource_invalid(self):
        for resource in [
            "ftp://testserver/oauth-mcp",
            "testserver/oauth-mcp",
            "http:///oauth-mcp",
            "http://other.example/oauth-mcp",
            "http://testserver/oauth-mcp?x=1",
            "http://testserver/oauth-mcp#x",
            "http://testserver/admin/",
            "http://testserver/nope",
        ]:
            assert discovery.validate_resource(resource) is None, resource

    def test_resource_metadata_url(self):
        assert (
            discovery.resource_metadata_url("https://example.com/shop/mcp")
            == "https://example.com/.well-known/oauth-protected-resource/shop/mcp"
        )
        assert (
            discovery.resource_metadata_url("https://example.com")
            == "https://example.com/.well-known/oauth-protected-resource"
        )


class URLsTests(SimpleTestCase):
    def test_endpoint_paths(self):
        paths = [str(pattern.pattern) for pattern in urls.urlpatterns]

        assert paths == ["authorize", "token", "register", "revoke"]

    def test_wellknown_paths(self):
        prefixes = [str(pattern.pattern) for pattern in wellknown.urlpatterns]
        assert prefixes == [".well-known/"]

        paths = [
            str(pattern.pattern) for pattern in wellknown.urlpatterns[0].url_patterns
        ]

        assert paths == [
            "oauth-authorization-server",
            "oauth-authorization-server/<path:issuer_path>",
            "oauth-protected-resource",
            "oauth-protected-resource/<path:resource_path>",
        ]


# -- Metadata documents


class MetadataTests(TestCase):
    def test_protected_resource(self):
        response = self.client.get("/.well-known/oauth-protected-resource/oauth-mcp")

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
            "resource_name": "OAuth server",
        }

    def test_protected_resource_name_without_title(self):
        response = self.client.get(
            "/.well-known/oauth-protected-resource/bearer-tokens-mcp"
        )

        assert response.json()["resource_name"] == "bearer-tokens-server"

    def test_protected_resource_not_a_server(self):
        for path in [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/admin/",
            "/.well-known/oauth-protected-resource/nope",
        ]:
            response = self.client.get(path)

            assert response.status_code == HTTPStatus.NOT_FOUND, path

    def test_protected_resource_names_request_scheme_and_host(self):
        response = self.client.get(
            "/.well-known/oauth-protected-resource/oauth-mcp", secure=True
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {
            "resource": "https://testserver/oauth-mcp",
            "authorization_servers": ["https://testserver/oauth"],
            "bearer_methods_supported": ["header"],
            "resource_name": "OAuth server",
        }

    def test_protected_resource_host_case(self):
        response = self.client.get(
            "/.well-known/oauth-protected-resource/oauth-mcp", HTTP_HOST="TESTSERVER"
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["resource"] == RESOURCE

    def test_authorization_server(self):
        response = self.client.get("/.well-known/oauth-authorization-server/oauth")

        assert response.status_code == HTTPStatus.OK
        document = response.json()
        assert document["issuer"] == ISSUER
        assert document["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert document["token_endpoint"] == f"{ISSUER}/token"
        assert document["registration_endpoint"] == f"{ISSUER}/register"
        assert document["revocation_endpoint"] == f"{ISSUER}/revoke"
        assert document["code_challenge_methods_supported"] == ["S256"]
        assert document["token_endpoint_auth_methods_supported"] == ["none"]
        assert document["grant_types_supported"] == [
            "authorization_code",
            "refresh_token",
        ]
        assert document["client_id_metadata_document_supported"] is True
        assert document["authorization_response_iss_parameter_supported"] is True

    @override_settings(MCPZ_OAUTH_DYNAMIC_REGISTRATION=False)
    def test_authorization_server_without_registration(self):
        response = self.client.get("/.well-known/oauth-authorization-server/oauth")

        assert "registration_endpoint" not in response.json()

    def test_authorization_server_wrong_path(self):
        for path in [
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/other",
            "/.well-known/oauth-authorization-server/oauth/authorize",
        ]:
            response = self.client.get(path)

            assert response.status_code == HTTPStatus.NOT_FOUND, path


# -- Authorization endpoint


class AuthorizeTests(TestCase):
    user: User
    oauth_client: Client

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", password="pw")
        cls.oauth_client = make_client(name="Claude")

    def setUp(self):
        self.client.force_login(self.user)

    def params(self, **overrides: Any) -> dict[str, Any]:
        _, challenge = pkce_pair()
        params: dict[str, Any] = {
            "response_type": "code",
            "client_id": self.oauth_client.client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "state": "xyz",
        }
        params.update(overrides)
        return {key: value for key, value in params.items() if value is not None}

    def get(self, **overrides: Any) -> Any:
        return self.client.get("/oauth/authorize", self.params(**overrides))

    def post(self, decision: str, **overrides: Any) -> Any:
        return self.client.post(
            "/oauth/authorize?" + urlencode(self.params(**overrides)),
            {"decision": decision},
        )

    def assert_redirect_error(self, response: Any, error: str) -> None:
        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.scheme == "http"
        assert parts.netloc == "localhost:9999"
        query = parse_qs(parts.query)
        assert query["error"] == [error]
        assert query["state"] == ["xyz"]
        assert query["iss"] == [ISSUER]
        assert "code" not in query

    def test_unknown_client(self):
        response = self.get(client_id="nope")

        assert response.status_code == HTTPStatus.BAD_REQUEST
        self.assertContains(
            response, "invalid_client", status_code=HTTPStatus.BAD_REQUEST
        )
        self.assertContains(
            response, "Unknown client_id.", status_code=HTTPStatus.BAD_REQUEST
        )

    def test_unregistered_redirect_uri(self):
        response = self.get(redirect_uri="http://localhost:9999/other")

        self.assertContains(
            response, "Unregistered redirect_uri.", status_code=HTTPStatus.BAD_REQUEST
        )

    def test_loopback_redirect_uri_any_port(self):
        self.oauth_client.redirect_uris = ["http://127.0.0.1:3000/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri="http://127.0.0.1:41234/callback")

        assert response.status_code == HTTPStatus.OK

    def test_loopback_redirect_uri_other_differences(self):
        self.oauth_client.redirect_uris = ["http://127.0.0.1:3000/callback"]
        self.oauth_client.save()

        for uri in [
            "http://127.0.0.1:3000/other",
            "https://127.0.0.1:3000/callback",
            "http://localhost:41234/callback",
            "http://127.0.0.1:3000/callback?x=1",
        ]:
            response = self.get(redirect_uri=uri)

            self.assertContains(
                response,
                "Unregistered redirect_uri.",
                status_code=HTTPStatus.BAD_REQUEST,
                msg_prefix=uri,
            )

    def test_missing_redirect_uri_single_registered(self):
        response = self.get(redirect_uri=None)

        assert response.status_code == HTTPStatus.OK

    def test_missing_redirect_uri_multiple_registered(self):
        self.oauth_client.redirect_uris.append("http://localhost:9999/other")
        self.oauth_client.save()

        response = self.get(redirect_uri=None)

        self.assertContains(
            response, "Missing redirect_uri.", status_code=HTTPStatus.BAD_REQUEST
        )

    def test_wrong_response_type(self):
        response = self.get(response_type="token")

        self.assert_redirect_error(response, "unsupported_response_type")

    def test_missing_code_challenge(self):
        response = self.get(code_challenge=None)

        self.assert_redirect_error(response, "invalid_request")

    def test_invalid_code_challenge(self):
        # Padding, and non-ASCII, which hmac.compare_digest cannot compare.
        for challenge in ["a" * 42 + "=", "\u00e9" * 43]:
            response = self.get(code_challenge=challenge)

            self.assert_redirect_error(response, "invalid_request")

    def test_scope_too_long(self):
        response = self.get(scope="x" * 501)

        self.assert_redirect_error(response, "invalid_scope")

    def test_plain_code_challenge_method(self):
        response = self.get(code_challenge_method="plain")

        self.assert_redirect_error(response, "invalid_request")

    def test_unknown_resource(self):
        for resource in [
            "http://testserver/other",
            "http://testserver/admin/",
            "http://other.example/oauth-mcp",
        ]:
            response = self.get(resource=resource)

            self.assert_redirect_error(response, "invalid_target")

    def test_missing_resource(self):
        response = self.get(resource=None)

        self.assert_redirect_error(response, "invalid_target")

    def test_login_required(self):
        self.client.logout()

        response = self.get()

        assert response.status_code == HTTPStatus.FOUND
        location = response["Location"]
        assert location.startswith("/accounts/login/?next=")
        assert "/oauth/authorize" in location

    def test_login_required_before_client_lookup(self):
        self.client.logout()

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.get(client_id=METADATA_URL)

        assert response.status_code == HTTPStatus.FOUND
        assert not fetch.called
        assert not Client.objects.filter(client_id=METADATA_URL).exists()

    def test_resource_case_insensitive(self):
        response = self.post("allow", resource="HTTP://TESTSERVER/oauth-mcp")

        assert response.status_code == HTTPStatus.FOUND
        assert AuthorizationCode.objects.get().resource == RESOURCE

    def test_consent_page(self):
        response = self.get(scope="mcp")

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Claude")
        self.assertContains(response, RESOURCE)
        self.assertContains(response, "alice")
        self.assertContains(response, "running on your computer")
        self.assertContains(response, "<code>mcp</code>")
        assert response["X-Frame-Options"] == "DENY"
        assert response["Content-Security-Policy"] == "frame-ancestors 'none'"

    def test_consent_page_without_auth_context_processor(self):
        templates = [{**settings.TEMPLATES[0], "OPTIONS": {}}]

        with self.settings(TEMPLATES=templates):
            response = self.get()

        self.assertContains(response, "<strong>alice</strong>")

    def test_consent_page_ipv6_local_redirect(self):
        self.oauth_client.redirect_uris = ["http://[::1]:9999/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri="http://[::1]:9999/callback")

        self.assertContains(response, "running on your computer")

    def test_consent_page_remote_redirect(self):
        self.oauth_client.redirect_uris = ["https://claude.ai/callback"]
        self.oauth_client.save()

        response = self.get(redirect_uri="https://claude.ai/callback")

        self.assertContains(response, "return to <strong>claude.ai</strong>")

    def test_deny(self):
        response = self.post("deny")

        self.assert_redirect_error(response, "access_denied")
        assert not AuthorizationCode.objects.exists()

    def test_allow(self):
        response = self.post("allow", scope="mcp")

        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.netloc == "localhost:9999"
        query = parse_qs(parts.query)
        assert query["state"] == ["xyz"]
        assert query["iss"] == [ISSUER]
        (value,) = query["code"]
        code = AuthorizationCode.objects.get()
        assert code.digest == digest_of(value)
        assert code.client == self.oauth_client
        assert code.user == self.user
        assert code.redirect_uri == REDIRECT_URI
        assert code.resource == RESOURCE
        assert code.scope == "mcp"
        assert code.used_at is None
        assert code.expires_at > timezone.now()

    def test_allow_without_state(self):
        response = self.post("allow", state=None)

        query = parse_qs(urlsplit(response["Location"]).query)
        assert "state" not in query
        assert "code" in query

    def test_redirect_uri_with_query(self):
        self.oauth_client.redirect_uris = ["http://localhost:9999/cb?app=1"]
        self.oauth_client.save()

        response = self.post("allow", redirect_uri="http://localhost:9999/cb?app=1")

        location = response["Location"]
        assert location.startswith("http://localhost:9999/cb?app=1&")

    def test_post_authorization_request(self):
        # RFC 6749 section 3.1: the request itself may be POSTed, from the
        # client's site, so without a CSRF token.
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        params = self.params()

        response = client.post("/oauth/authorize", params)

        assert response.status_code == HTTPStatus.FOUND
        parts = urlsplit(response["Location"])
        assert parts.path == "/oauth/authorize"
        assert parse_qs(parts.query) == {key: [value] for key, value in params.items()}
        response = client.get(response["Location"])
        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Allow access?")

    def test_consent_with_csrf(self):
        # As a browser does it: the page sets the cookie and carries the
        # token, without relying on the project's middleware.
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        params = self.params()
        page = client.get("/oauth/authorize", params)
        assert "csrftoken" in client.cookies

        response = client.post(
            "/oauth/authorize?" + urlencode(params),
            {"decision": "allow", "csrfmiddlewaretoken": page.context["csrf_token"]},
        )

        assert response.status_code == HTTPStatus.FOUND
        assert "code=" in response["Location"]

    def test_post_requires_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            "/oauth/authorize?" + urlencode(self.params()), {"decision": "allow"}
        )

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_metadata_document_client(self):
        document = {
            "client_id": METADATA_URL,
            "client_name": "Metadata client",
            "redirect_uris": [REDIRECT_URI],
        }

        with mock_metadata_fetch(document):
            response = self.get(client_id=METADATA_URL)

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Metadata client")
        client = Client.objects.get(client_id=METADATA_URL)
        assert client.kind == Client.Kind.METADATA

    def test_metadata_document_client_invalid(self):
        with mock_metadata_fetch({"client_id": "https://other.example/x"}):
            response = self.get(client_id=METADATA_URL)

        self.assertContains(
            response, "does not match its URL", status_code=HTTPStatus.BAD_REQUEST
        )


# -- Token endpoint


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
            digest=digest_of(value), code_challenge=challenge, **fields
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
        access = AccessToken.objects.get(digest=digest_of(body["access_token"]))
        refresh = RefreshToken.objects.get(digest=digest_of(body["refresh_token"]))
        assert refresh.access_token == access
        assert access.is_valid
        assert refresh.is_valid
        return body


class TokenCodeTests(TokenTestCase):
    def test_success(self):
        code, value, verifier = self.make_code(scope="mcp")

        response = self.exchange(value, verifier, redirect_uri=REDIRECT_URI)

        body = self.assert_tokens(response)
        assert body["scope"] == "mcp"
        code.refresh_from_db()
        assert code.used_at is not None
        access = AccessToken.objects.get()
        assert access.user == self.user
        assert access.client == self.oauth_client
        assert access.code == code
        assert access.resource == RESOURCE
        assert access.scope == "mcp"
        self.oauth_client.refresh_from_db()
        assert self.oauth_client.last_used_at is not None

    def test_success_without_scope(self):
        _, value, verifier = self.make_code()

        body = self.assert_tokens(self.exchange(value, verifier))

        assert "scope" not in body

    def test_get_not_allowed(self):
        response = self.client.get("/oauth/token")

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED

    def test_unknown_client(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, client_id="nope")

        self.assert_token_error(response, "invalid_client", HTTPStatus.UNAUTHORIZED)

    def test_unsupported_grant_type(self):
        response = self.post_token(grant_type="password")

        self.assert_token_error(response, "unsupported_grant_type")

    def test_metadata_client_known(self):
        client = make_client(client_id=METADATA_URL, kind=Client.Kind.METADATA)
        _, value, verifier = self.make_code(client=client)

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.exchange(value, verifier, client_id=METADATA_URL)

        self.assert_tokens(response)
        assert not fetch.called

    def test_metadata_client_unknown_not_fetched(self):
        _, value, verifier = self.make_code()

        with mock.patch.object(cimd, "fetch_document") as fetch:
            response = self.exchange(value, verifier, client_id=METADATA_URL)

        self.assert_token_error(response, "invalid_client", HTTPStatus.UNAUTHORIZED)
        assert not fetch.called
        assert not Client.objects.filter(client_id=METADATA_URL).exists()

    def test_unknown_code(self):
        response = self.exchange("mcp_nope", "v" * 43)

        self.assert_token_error(response, "invalid_grant")

    def test_code_for_other_client(self):
        other = make_client()
        _, value, verifier = self.make_code(client=other)

        response = self.exchange(value, verifier)

        self.assert_token_error(response, "invalid_grant")

    def test_code_reuse_revokes_family(self):
        code, value, verifier = self.make_code()
        first = self.assert_tokens(self.exchange(value, verifier))

        response = self.exchange(value, verifier)

        body = self.assert_token_error(response, "invalid_grant")
        assert body["error_description"] == "Authorization code already used."
        access = AccessToken.objects.get(digest=digest_of(first["access_token"]))
        refresh = RefreshToken.objects.get(digest=digest_of(first["refresh_token"]))
        assert not access.is_valid
        assert not refresh.is_valid

    def test_code_expired(self):
        _, value, verifier = self.make_code(
            expires_at=timezone.now() - dt.timedelta(seconds=1)
        )

        response = self.exchange(value, verifier)

        body = self.assert_token_error(response, "invalid_grant")
        assert body["error_description"] == "Authorization code expired."

    def test_redirect_uri_mismatch(self):
        _, value, verifier = self.make_code()

        response = self.exchange(
            value, verifier, redirect_uri="http://localhost:9999/other"
        )

        self.assert_token_error(response, "invalid_grant")

    def test_resource_mismatch(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, resource="http://testserver/other")

        self.assert_token_error(response, "invalid_target")

    def test_resource_match(self):
        _, value, verifier = self.make_code()

        response = self.exchange(value, verifier, resource=RESOURCE)

        self.assert_tokens(response)

    def test_wrong_verifier(self):
        _, value, _ = self.make_code()

        response = self.exchange(value, "w" * 43)

        self.assert_token_error(response, "invalid_grant")

    def test_short_verifier(self):
        _, value, _ = self.make_code()

        response = self.exchange(value, "short")

        self.assert_token_error(response, "invalid_grant")

    def test_inactive_user(self):
        _, value, verifier = self.make_code()
        self.user.is_active = False
        self.user.save()

        response = self.exchange(value, verifier)

        self.assert_token_error(response, "invalid_grant")


class TokenRefreshTests(TokenTestCase):
    def issue(self, **fields: Any) -> tuple[dict[str, Any], AuthorizationCode]:
        code, value, verifier = self.make_code(**fields)
        return self.assert_tokens(self.exchange(value, verifier)), code

    def refresh(self, refresh_token: str, **data: Any) -> Any:
        data.setdefault("grant_type", "refresh_token")
        return self.post_token(refresh_token=refresh_token, **data)

    def test_success(self):
        first, code = self.issue(scope="mcp")

        response = self.refresh(first["refresh_token"])

        second = self.assert_tokens(response)
        assert second["scope"] == "mcp"
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        old_access = AccessToken.objects.get(digest=digest_of(first["access_token"]))
        old_refresh = RefreshToken.objects.get(digest=digest_of(first["refresh_token"]))
        assert old_access.is_valid, "Left to expire, for requests in flight."
        assert old_refresh.used_at is not None
        new_access = AccessToken.objects.get(digest=digest_of(second["access_token"]))
        assert new_access.code == code

    def test_unknown(self):
        response = self.refresh("mcp_nope")

        self.assert_token_error(response, "invalid_grant")

    def test_other_client(self):
        first, _ = self.issue()
        other = make_client()

        response = self.refresh(first["refresh_token"], client_id=other.client_id)

        self.assert_token_error(response, "invalid_grant")

    def test_reuse_revokes_family(self):
        first, _ = self.issue()
        second = self.assert_tokens(self.refresh(first["refresh_token"]))

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")
        access = AccessToken.objects.get(digest=digest_of(second["access_token"]))
        refresh = RefreshToken.objects.get(digest=digest_of(second["refresh_token"]))
        assert not access.is_valid
        assert not refresh.is_valid

    def test_reuse_without_code_revokes_pair(self):
        first, code = self.issue()
        second = self.assert_tokens(self.refresh(first["refresh_token"]))
        code.delete()

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")
        access = AccessToken.objects.get(digest=digest_of(second["access_token"]))
        assert access.is_valid, "Only the reused token's own pair is revoked."
        old = RefreshToken.objects.get(digest=digest_of(first["refresh_token"]))
        assert not old.is_valid

    def test_expired(self):
        first, _ = self.issue()
        RefreshToken.objects.update(expires_at=timezone.now() - dt.timedelta(days=1))

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")

    def test_resource_mismatch(self):
        first, _ = self.issue()

        response = self.refresh(
            first["refresh_token"], resource="http://testserver/other"
        )

        self.assert_token_error(response, "invalid_target")

    def test_inactive_user(self):
        first, _ = self.issue()
        self.user.is_active = False
        self.user.save()

        response = self.refresh(first["refresh_token"])

        self.assert_token_error(response, "invalid_grant")


# -- Registration


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


# -- Revocation


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


# -- The auth callable


class OAuthAuthTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def request(
        self, authorization: str | None, path: str = "/oauth-mcp"
    ) -> HttpRequest:
        headers = {} if authorization is None else {"Authorization": authorization}
        return RequestFactory().post(path, headers=headers)

    def assert_challenge(
        self, response: HttpResponse | None, error: str | None = None
    ) -> None:
        assert response is not None
        assert response.status_code == HTTPStatus.UNAUTHORIZED
        expected = (
            'Bearer resource_metadata="http://testserver/.well-known/'
            'oauth-protected-resource/oauth-mcp"'
        )
        if error is not None:
            expected = expected.replace("Bearer ", f'Bearer error="{error}", ')
        assert response.headers["WWW-Authenticate"] == expected

    def test_missing_header(self):
        self.assert_challenge(oauth_auth(self.request(None)))

    def test_wrong_scheme(self):
        self.assert_challenge(oauth_auth(self.request("Basic abc")))

    def test_empty_credential(self):
        self.assert_challenge(oauth_auth(self.request("Bearer ")))

    def test_unknown_token(self):
        self.assert_challenge(
            oauth_auth(self.request("Bearer mcp_nope")), "invalid_token"
        )

    def test_expired_token(self):
        _, value = make_access_token(user=self.user, lifetime=dt.timedelta(seconds=-1))

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_revoked_token(self):
        token, value = make_access_token(user=self.user)
        token.revoke()

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_wrong_resource(self):
        _, value = make_access_token(user=self.user, resource="http://testserver/other")

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_inactive_user(self):
        _, value = make_access_token(user=self.user)
        self.user.is_active = False
        self.user.save()

        self.assert_challenge(
            oauth_auth(self.request(f"Bearer {value}")), "invalid_token"
        )

    def test_valid(self):
        token, value = make_access_token(user=self.user)
        request = self.request(f"bearer {value}")

        assert oauth_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]
        assert request.user == self.user

    def test_host_case_insensitive(self):
        token, value = make_access_token(user=self.user)
        request = RequestFactory().post(
            "/oauth-mcp",
            headers={"Authorization": f"Bearer {value}"},
            SERVER_NAME="TESTSERVER",
        )

        assert oauth_auth(request) is None
        assert request.mcp_token == token  # type: ignore[attr-defined]


class OAuthServerTests(ServerTestCase, TestCase):
    """The oauth app authenticating a server, end to end."""

    url = "/oauth-mcp"
    user: User

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")

    def test_unauthenticated(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert "resource_metadata=" in response.headers["WWW-Authenticate"]

    def test_whoami(self):
        _, value = make_access_token(user=self.user, client=make_client(name="Claude"))

        response = self.post(
            make_message("tools/call", {"name": "oauth_whoami", "arguments": {}}),
            headers={"Authorization": f"Bearer {value}"},
        )

        assert response.json()["result"]["structuredContent"] == {
            "client": "Claude",
            "user": "alice",
        }


# -- Client ID metadata documents


@contextlib.contextmanager
def local_https_server(
    routes: dict[str, tuple[int, bytes]],
) -> Iterator[tuple[int, ssl.SSLContext]]:
    """
    Serve fixed responses over TLS on a local port, for testing the fetcher.

    Yields the port and a client context that trusts the server's
    self-signed certificate for the name "localhost".
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = routes[self.path]
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/doc")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    with tempfile.TemporaryDirectory() as directory:
        cert = f"{directory}/cert.pem"
        key = f"{directory}/key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=cert)
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, client_context
        finally:
            server.shutdown()
            server.server_close()


PUBLIC_ADDRESS = "93.184.216.34"


def mock_metadata_fetch(document: Any, *, body: bytes | None = None) -> Any:
    """Patch resolution and the fetch to return the document from a public host."""
    if body is None:
        body = json.dumps(document).encode()
    return mock.patch.multiple(
        cimd,
        resolve_public_address=mock.Mock(return_value=PUBLIC_ADDRESS),
        _fetch=mock.Mock(return_value=body),
    )


class MetadataDocumentTests(TestCase):
    document = {
        "client_id": METADATA_URL,
        "client_name": "Metadata client",
        "redirect_uris": [REDIRECT_URI],
    }

    def test_is_metadata_url(self):
        assert cimd.is_metadata_url(METADATA_URL)
        assert not cimd.is_metadata_url("abc123")
        assert not cimd.is_metadata_url("http://client.example/client.json")
        assert not cimd.is_metadata_url("https://client.example")
        assert not cimd.is_metadata_url("https://client.example/")
        assert not cimd.is_metadata_url("https://client.example/" + "a" * 500)

    def test_fetch_and_cache(self):
        fetch = mock.Mock(return_value=json.dumps(self.document).encode())

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", fetch),
        ):
            client = cimd.get_metadata_client(METADATA_URL)
            again = cimd.get_metadata_client(METADATA_URL)

        assert client == again
        assert client.name == "Metadata client"
        assert client.redirect_uris == [REDIRECT_URI]
        assert client.fetched_at is not None
        fetch.assert_called_once_with(
            "client.example", 443, PUBLIC_ADDRESS, "/oauth/client.json"
        )

    def test_fetch_path_with_query(self):
        url = METADATA_URL + "?v=2"
        body = json.dumps({**self.document, "client_id": url}).encode()
        fetch = mock.Mock(return_value=body)

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", fetch),
        ):
            cimd.fetch_document(url)

        fetch.assert_called_once_with(
            "client.example", 443, PUBLIC_ADDRESS, "/oauth/client.json?v=2"
        )

    def test_stale_cache_kept_when_fetch_fails(self):
        stale = timezone.now() - dt.timedelta(hours=2)
        Client.objects.create(
            client_id=METADATA_URL,
            kind=Client.Kind.METADATA,
            name="Old name",
            redirect_uris=[REDIRECT_URI],
            fetched_at=stale,
        )

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("down")),
        ):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "Old name"
        assert client.fetched_at == stale

    def test_no_cache_when_fetch_fails(self):
        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("down")),
            pytest.raises(cimd.MetadataError, match="Could not fetch"),
        ):
            cimd.get_metadata_client(METADATA_URL)

    def test_refetch_after_cache_lifetime(self):
        stale = timezone.now() - dt.timedelta(hours=2)
        Client.objects.create(
            client_id=METADATA_URL,
            kind=Client.Kind.METADATA,
            name="Old name",
            redirect_uris=[],
            fetched_at=stale,
        )

        with mock_metadata_fetch(self.document):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "Metadata client"
        assert client.fetched_at is not None
        assert client.fetched_at > stale

    def test_fragment_rejected(self):
        with pytest.raises(cimd.MetadataError, match="HTTPS URL with a path"):
            cimd.fetch_document(METADATA_URL + "#x")

    def test_fetch_error(self):
        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("boom")),
            pytest.raises(cimd.MetadataError, match="Could not fetch"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_too_large(self):
        with (
            mock_metadata_fetch(None, body=b"x" * (cimd.MAX_BYTES + 1)),
            pytest.raises(cimd.MetadataError, match="too large"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_invalid_json(self):
        with (
            mock_metadata_fetch(None, body=b"nope"),
            pytest.raises(cimd.MetadataError, match="not valid JSON"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_not_object(self):
        with (
            mock_metadata_fetch([]),
            pytest.raises(cimd.MetadataError, match="not a JSON object"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_client_id_mismatch(self):
        with (
            mock_metadata_fetch({**self.document, "client_id": "https://x.example/y"}),
            pytest.raises(cimd.MetadataError, match="does not match"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_missing_name(self):
        with (
            mock_metadata_fetch({**self.document, "client_name": ""}),
            pytest.raises(cimd.MetadataError, match="client_name"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_missing_redirect_uris(self):
        with (
            mock_metadata_fetch({**self.document, "redirect_uris": []}),
            pytest.raises(cimd.MetadataError, match="list of redirect_uris"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_bad_redirect_uris(self):
        for uris in [
            [1],
            ["http://client.example/cb"],
            ["myapp://callback"],
            ["https://client.example/cb#frag"],
            ["https://client.example/" + "a" * 500],
        ]:
            with (
                mock_metadata_fetch({**self.document, "redirect_uris": uris}),
                pytest.raises(cimd.MetadataError, match="redirect_uris"),
            ):
                cimd.fetch_document(METADATA_URL)

    def test_long_name_truncated(self):
        with mock_metadata_fetch({**self.document, "client_name": "n" * 300}):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "n" * 200

    def test_confidential_client(self):
        with (
            mock_metadata_fetch(
                {**self.document, "token_endpoint_auth_method": "private_key_jwt"}
            ),
            pytest.raises(cimd.MetadataError, match="public clients"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_fetch_over_tls(self):
        routes = {"/doc": (200, b"{}"), "/moved": (302, b""), "/missing": (404, b"")}

        with local_https_server(routes) as (port, context):
            body = cimd._fetch("localhost", port, "127.0.0.1", "/doc", context=context)
            with pytest.raises(cimd.MetadataError, match="status 302"):
                cimd._fetch("localhost", port, "127.0.0.1", "/moved", context=context)
            with pytest.raises(cimd.MetadataError, match="status 404"):
                cimd._fetch("localhost", port, "127.0.0.1", "/missing", context=context)

        assert body == b"{}"

    def test_resolve_public_address(self):
        results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:21f:cb07::1", 443, 0, 0),
            ),
        ]

        with mock.patch.object(socket, "getaddrinfo", return_value=results):
            address = cimd.resolve_public_address("client.example", 443)

        assert address == "93.184.216.34"

    def test_resolve_public_address_scoped(self):
        results = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 443, 0, 2))
        ]

        with (
            mock.patch.object(socket, "getaddrinfo", return_value=results),
            pytest.raises(cimd.MetadataError, match="non-public"),
        ):
            cimd.resolve_public_address("client.example", 443)

    def test_resolve_public_address_private(self):
        results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

        with (
            mock.patch.object(socket, "getaddrinfo", return_value=results),
            pytest.raises(cimd.MetadataError, match="non-public"),
        ):
            cimd.resolve_public_address("client.example", 443)

    def test_resolve_public_address_unresolvable(self):
        with (
            mock.patch.object(socket, "getaddrinfo", side_effect=OSError),
            pytest.raises(cimd.MetadataError, match="Could not resolve"),
        ):
            cimd.resolve_public_address("client.example", 443)


# -- Admin


class AdminTests(TestCase):
    admin: User

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", password="pw")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_client_list(self):
        make_client(name="Claude")

        response = self.client.get("/admin/django_mcpz_oauth/client/")

        self.assertContains(response, "Claude")

    def test_client_no_add(self):
        response = self.client.get("/admin/django_mcpz_oauth/client/add/")

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_token_list_and_revoke(self):
        token, _ = make_access_token(user=self.admin)

        response = self.client.get("/admin/django_mcpz_oauth/accesstoken/")
        assert response.status_code == HTTPStatus.OK

        response = self.client.post(
            "/admin/django_mcpz_oauth/accesstoken/",
            {"action": "revoke", "_selected_action": [token.pk]},
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        token.refresh_from_db()
        assert not token.is_valid
        (message,) = list(response.context["messages"])
        assert str(message) == "Revoked 1 token(s)."

    def test_refresh_token_list_and_change(self):
        access, _ = make_access_token(user=self.admin)
        refresh, _ = RefreshToken.create(
            lifetime=dt.timedelta(days=1),
            client=access.client,
            user=self.admin,
            resource=RESOURCE,
            access_token=access,
        )

        response = self.client.get("/admin/django_mcpz_oauth/refreshtoken/")
        assert response.status_code == HTTPStatus.OK
        response = self.client.get(
            f"/admin/django_mcpz_oauth/refreshtoken/{refresh.pk}/change/"
        )

        assert response.status_code == HTTPStatus.OK
        self.assertContains(response, "Used at")

    def test_revoked_filter(self):
        response = self.client.get(
            "/admin/django_mcpz_oauth/accesstoken/?revoked_at__isempty=1"
        )

        assert response.status_code == HTTPStatus.OK


# -- Management command


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


# -- Model strings


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
