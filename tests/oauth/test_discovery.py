from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase

from django_mcpz.oauth import discovery
from tests.oauth.utils import ISSUER, RESOURCE


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
        from tests.mcp import oauth_server, server

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
