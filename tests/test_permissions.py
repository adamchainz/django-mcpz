from __future__ import annotations

from typing import Any

import msgspec.json
import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import RequestFactory, TestCase

from django_mcpz.jsonrpc import INVALID_PARAMS
from django_mcpz.server import PROTOCOL_VERSION, MCPServer, public
from tests.test_server import ServerTestCase, make_message


class PermissionTests(ServerTestCase, TestCase):
    url = "/perms-mcp"

    @classmethod
    def setUpTestData(cls):
        User.objects.create_user("staff", is_staff=True)
        viewer = User.objects.create_user("viewer")
        viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="tests", codename="view_widget"
            )
        )
        User.objects.create_user("nobody")

    def list_tools(self, username: str | None = None) -> list[str]:
        response = self.post(make_message("tools/list"), headers={"X-User": username})
        assert response.status_code == 200
        return [tool["name"] for tool in response.json()["result"]["tools"]]

    def call(self, name: str, username: str | None = None) -> Any:
        return self.post(
            make_message("tools/call", {"name": name, "arguments": {}}),
            headers={"X-User": username},
        )

    def test_list_anonymous(self):
        assert self.list_tools() == ["public_tool"]

    def test_list_callable_permission(self):
        assert self.list_tools("staff") == ["public_tool", "staff_tool"]

    def test_list_string_permission(self):
        assert self.list_tools("viewer") == ["public_tool", "widget_tool"]

    def test_list_no_permissions(self):
        assert self.list_tools("nobody") == ["public_tool"]

    def test_call_unrestricted(self):
        response = self.call("public_tool")

        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "public"}]

    def test_call_permitted(self):
        response = self.call("staff_tool", "staff")

        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "staff"}]

    def test_call_string_permission(self):
        response = self.call("widget_tool", "viewer")

        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "widgets"}]

    def test_call_denied(self):
        # Indistinguishable from a nonexistent tool, matching its absence
        # from tools/list.
        response = self.call("staff_tool", "nobody")

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Unknown tool: 'staff_tool'"

    def test_call_denied_anonymous(self):
        response = self.call("widget_tool")

        self.assert_error(response, INVALID_PARAMS)

    def test_string_permission_without_user(self):
        # The public auth callable sets no user, so a string permission cannot
        # be checked: fail loudly rather than with an AttributeError.
        server = MCPServer(name="no-user", version="1.0.0", auth=public)

        @server.tool(description="Restricted.", permission="tests.view_widget")
        def restricted(request: HttpRequest) -> None:  # pragma: no cover
            return None

        request = RequestFactory().post(
            "/mcp",
            data=msgspec.json.encode(make_message("tools/list")),
            content_type="application/json",
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "tools/list",
            },
        )

        with pytest.raises(ImproperlyConfigured, match="needs request.user"):
            server(request)
