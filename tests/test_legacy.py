from __future__ import annotations

from http import HTTPStatus
from typing import Any

import msgspec.json

from django_mcpz.jsonrpc import INVALID_PARAMS, METHOD_NOT_FOUND
from django_mcpz.legacy import LEGACY_PROTOCOL_VERSIONS
from django_mcpz.server import HEADER_MISMATCH, PROTOCOL_VERSION
from tests.test_server import ServerTestCase, make_message


class LegacyTests(ServerTestCase):
    """Clients on the 2025 revisions, served in their stateless mode."""

    def legacy_post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        version: str | None = None,
        id: Any = 1,
    ) -> Any:
        # No _meta, and none of the 2026 headers.
        message = make_message(
            method, params, id=id, protocol_version=None, client_capabilities=None
        )
        return self.post(
            message,
            headers={
                "MCP-Protocol-Version": version,
                "Mcp-Method": None,
                "Mcp-Name": None,
            },
        )

    def assert_legacy_result(self, response, *, id=1):
        assert response.status_code == HTTPStatus.OK
        assert "Mcp-Session-Id" not in response.headers
        data = response.json()
        assert data["id"] == id
        return data["result"]

    def test_initialize(self):
        response = self.legacy_post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "old-client", "version": "1.0"},
            },
        )

        result = self.assert_legacy_result(response)
        assert result == {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "example-server",
                "version": "1.2.3",
                "title": "Example Server",
                "description": "Exercises every feature of django-mcpz.",
                "websiteUrl": "https://github.com/adamchainz/django-mcpz",
                "icons": [
                    {
                        "src": "http://testserver/static/diner/icon.png",
                        "mimeType": "image/png",
                        "sizes": ["48x48"],
                    },
                    {
                        "src": "data:image/svg+xml,"
                        + "%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"
                    },
                ],
            },
            "instructions": "Example MCP server used in the django-mcpz test suite.",
        }

    def test_initialize_without_instructions(self):
        response = self.client.post(
            "/perms-mcp",
            data=msgspec.json.encode(
                make_message(
                    "initialize",
                    {"protocolVersion": "2025-11-25", "capabilities": {}},
                    protocol_version=None,
                    client_capabilities=None,
                )
            ),
            content_type="application/json",
        )

        result = self.assert_legacy_result(response)
        assert "instructions" not in result

    def test_initialize_unknown_version(self):
        # Offer the latest legacy revision. The client decides.
        response = self.legacy_post(
            "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}
        )

        result = self.assert_legacy_result(response)
        assert result["protocolVersion"] == LEGACY_PROTOCOL_VERSIONS[0]

    def test_initialize_missing_version(self):
        response = self.legacy_post("initialize", {"capabilities": {}})

        result = self.assert_legacy_result(response)
        assert result["protocolVersion"] == LEGACY_PROTOCOL_VERSIONS[0]

    def test_initialized_notification(self):
        response = self.legacy_post("notifications/initialized", id=None)

        assert response.status_code == HTTPStatus.ACCEPTED

    def test_ping(self):
        response = self.legacy_post("ping")

        assert self.assert_legacy_result(response) == {}

    def test_tools_list_no_header(self):
        # 2025-03-26 clients send no version header.
        response = self.legacy_post("tools/list")

        result = self.assert_legacy_result(response)
        assert [tool["name"] for tool in result["tools"]][:2] == ["add", "greet"]

    def test_tools_list_with_header(self):
        response = self.legacy_post("tools/list", version="2025-11-25")

        result = self.assert_legacy_result(response)
        assert len(result["tools"]) == 17

    def test_tools_call(self):
        response = self.legacy_post(
            "tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}
        )

        result = self.assert_legacy_result(response)
        assert result["isError"] is False
        assert result["structuredContent"] == {"sum": 3}

    def test_tools_call_nested_arguments(self):
        response = self.legacy_post(
            "tools/call",
            {
                "name": "regional",
                "arguments": {"region": "eu", "config": {"zone": "a"}},
            },
        )

        result = self.assert_legacy_result(response)
        assert result["structuredContent"] == {"region": "eu"}

    def test_tools_call_unknown_tool(self):
        response = self.legacy_post("tools/call", {"name": "nope"})

        self.assert_error(response, INVALID_PARAMS)

    def test_unknown_method(self):
        # Legacy transports expect JSON-RPC errors in 200 responses.
        response = self.legacy_post("resources/list")

        error = self.assert_error(response, METHOD_NOT_FOUND)
        assert error["message"] == "Method not found: 'resources/list'"

    def test_discover_without_header(self):
        response = self.legacy_post("server/discover")

        result = self.assert_legacy_result(response)
        assert result["supportedVersions"][0] == PROTOCOL_VERSION

    def test_unsupported_version_header(self):
        response = self.legacy_post("tools/list", version="2024-11-05")

        self.assert_error(response, -32022, status=HTTPStatus.BAD_REQUEST)


class StrictTests(ServerTestCase):
    """With minimum_protocol_version="2026-07-28", only that revision is served."""

    url = "/strict-mcp"

    def test_missing_protocol_version_header(self):
        response = self.post(
            make_message("tools/list"),
            headers={"MCP-Protocol-Version": None},
        )

        error = self.assert_error(
            response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST
        )
        assert "MCP-Protocol-Version" in error["message"]

    def test_legacy_version_header(self):
        response = self.post(
            make_message("tools/list"),
            headers={"MCP-Protocol-Version": "2025-11-25"},
        )

        self.assert_error(response, -32022, status=HTTPStatus.BAD_REQUEST)

    def test_initialize(self):
        response = self.post(
            make_message("initialize", protocol_version=None, client_capabilities=None),
            headers={"MCP-Protocol-Version": None, "Mcp-Method": None},
        )

        self.assert_error(response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST)

    def test_current_revision_served(self):
        response = self.post(
            make_message("tools/call", {"name": "strict_noop", "arguments": {}})
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["result"]["isError"] is False

    def test_discover(self):
        response = self.post(make_message("server/discover"))

        assert response.json()["result"]["supportedVersions"] == [PROTOCOL_VERSION]
