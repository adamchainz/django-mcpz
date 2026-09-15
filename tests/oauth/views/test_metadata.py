from __future__ import annotations

from http import HTTPStatus

from django.test import TestCase, override_settings
from django.test.utils import override_script_prefix

from tests.oauth.utils import ISSUER, RESOURCE


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
            "/.well-known/oauth-protected-resource/oauth-mcp",
            headers={"host": "TESTSERVER"},
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["resource"] == RESOURCE

    # The site deployed under a path prefix: the documents keep their
    # RFC-defined places at the host root, with the prefix in the paths they
    # carry, so the deployment must route them to the site.
    @override_settings(FORCE_SCRIPT_NAME="/app")
    @override_script_prefix("/app/")
    def test_under_prefix(self):
        response = self.client.get(
            "/.well-known/oauth-protected-resource/app/oauth-mcp"
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["resource"] == "http://testserver/app/oauth-mcp"
        assert response.json()["authorization_servers"] == [
            "http://testserver/app/oauth"
        ]

        response = self.client.get("/.well-known/oauth-protected-resource/oauth-mcp")

        assert response.status_code == HTTPStatus.NOT_FOUND

        response = self.client.get("/.well-known/oauth-authorization-server/app/oauth")

        assert response.status_code == HTTPStatus.OK
        assert response.json()["issuer"] == "http://testserver/app/oauth"

        response = self.client.get("/.well-known/oauth-authorization-server/oauth")

        assert response.status_code == HTTPStatus.NOT_FOUND

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
