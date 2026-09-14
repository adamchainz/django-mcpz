from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Annotated, Any, TypedDict
from unittest import mock

import msgspec
import msgspec.json
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)

from django_mcpz.headers import encode_header_value
from django_mcpz.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from django_mcpz.server import (
    HEADER_MISMATCH,
    PROTOCOL_VERSION,
    UNSUPPORTED_PROTOCOL_VERSION,
    Icon,
    MCPServer,
    public,
)
from tests import mcp
from tests.models import Widget

if TYPE_CHECKING:
    # For the unresolvable-annotation test: known to mypy, absent at runtime.
    Missing = int

SERVER_INFO = {"name": "example-server", "version": "1.2.3", "title": "Example Server"}


# Module level, since msgspec resolves deferred annotations in module scope.


class Node(msgspec.Struct):
    name: str
    children: list[Node] = []


class Routed(msgspec.Struct):
    region: Annotated[str, msgspec.Meta(extra_json_schema={"x-mcp-header": "Region"})]


def make_message(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    id: Any = 1,
    protocol_version: str | None = PROTOCOL_VERSION,
    client_capabilities: Any = ...,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if protocol_version is not None:
        meta["io.modelcontextprotocol/protocolVersion"] = protocol_version
    if client_capabilities is ...:
        client_capabilities = {}
    if client_capabilities is not None:
        meta["io.modelcontextprotocol/clientCapabilities"] = client_capabilities
    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {**(params or {}), "_meta": meta},
    }
    if id is not None:
        message["id"] = id
    return message


class ServerTestCase(SimpleTestCase):
    url = "/mcp"

    def post(
        self, message: dict[str, Any], headers: dict[str, str | None] | None = None
    ) -> Any:
        final_headers: dict[str, str | None] = {
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        method = message.get("method")
        if isinstance(method, str):
            final_headers["Mcp-Method"] = method
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            final_headers["Mcp-Name"] = encode_header_value(params["name"])
        if headers:
            final_headers.update(headers)
        return self.client.post(
            self.url,
            data=msgspec.json.encode(message),
            content_type="application/json",
            headers={
                name: value
                for name, value in final_headers.items()
                if value is not None
            },
        )

    def assert_error(self, response, code, *, id=1, status=HTTPStatus.OK):
        assert response.status_code == status
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == id
        assert data["error"]["code"] == code
        return data["error"]

    def assert_result(self, response, *, id=1):
        assert response.status_code == HTTPStatus.OK
        assert response.headers["Content-Type"] == "application/json"
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == id
        result = data["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == SERVER_INFO
        return result


class TransportTests(ServerTestCase):
    def test_get_not_allowed(self):
        response = self.client.get(self.url)

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
        assert response.headers["Allow"] == "POST"

    def test_delete_not_allowed(self):
        response = self.client.delete(self.url)

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED

    def test_csrf_exempt(self):
        # MCP clients are not browsers: no CSRF token is required.
        client = Client(enforce_csrf_checks=True)

        with override_settings(
            MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"]
        ):
            response = client.post(
                self.url,
                data=msgspec.json.encode(make_message("server/discover")),
                content_type="application/json",
                headers={
                    "MCP-Protocol-Version": PROTOCOL_VERSION,
                    "Mcp-Method": "server/discover",
                },
            )

        assert response.status_code == HTTPStatus.OK

    def test_origin_disallowed(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Origin": "https://evil.example.com"},
        )

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert response.json()["error"]["code"] == INVALID_REQUEST

    def test_origin_unparsable(self):
        response = self.post(make_message("tools/list"), headers={"Origin": "null"})

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_origin_invalid_ipv6(self):
        response = self.post(
            make_message("tools/list"), headers={"Origin": "https://[::1"}
        )

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_origin_allowed(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Origin": "http://testserver"},
        )

        assert response.status_code == HTTPStatus.OK

    @override_settings(DEBUG=True, ALLOWED_HOSTS=[])
    def test_origin_allowed_debug_localhost(self):
        # server/discover rather than tools/list: resolving tool icons calls
        # request.build_absolute_uri(), which validates the host header
        # against the overridden empty ALLOWED_HOSTS.
        response = self.post(
            make_message("server/discover"),
            headers={"Origin": "http://localhost:8000"},
        )

        assert response.status_code == HTTPStatus.OK

    def test_invalid_json(self):
        response = self.client.post(
            self.url, data=b"{not json", content_type="application/json"
        )

        self.assert_error(response, PARSE_ERROR, id=None, status=HTTPStatus.BAD_REQUEST)

    def test_not_a_json_object(self):
        response = self.client.post(
            self.url, data=b"[1, 2]", content_type="application/json"
        )

        self.assert_error(
            response, INVALID_REQUEST, id=None, status=HTTPStatus.BAD_REQUEST
        )

    def test_wrong_jsonrpc_version(self):
        response = self.client.post(
            self.url,
            data=b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}',
            content_type="application/json",
        )

        # The id was readable, so it is echoed in the error response.
        self.assert_error(response, INVALID_REQUEST, status=HTTPStatus.BAD_REQUEST)

    def test_missing_method(self):
        response = self.post({"jsonrpc": "2.0", "id": 1})

        self.assert_error(response, INVALID_REQUEST, status=HTTPStatus.BAD_REQUEST)

    def test_missing_params(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})

        self.assert_error(response, INVALID_PARAMS, status=HTTPStatus.BAD_REQUEST)

    def test_notification_accepted(self):
        message = make_message("notifications/whatever", id=None)

        response = self.post(message)

        assert response.status_code == HTTPStatus.ACCEPTED
        assert response.content == b""

    def test_notification_skips_header_checks(self):
        message = make_message("notifications/whatever", id=None)

        response = self.post(
            message,
            headers={"MCP-Protocol-Version": None, "Mcp-Method": None},
        )

        assert response.status_code == HTTPStatus.ACCEPTED

    def test_null_id_invalid(self):
        message = make_message("tools/list")
        message["id"] = None

        response = self.post(message)

        self.assert_error(
            response, INVALID_REQUEST, id=None, status=HTTPStatus.BAD_REQUEST
        )

    def test_boolean_id_invalid(self):
        response = self.post(make_message("tools/list", id=True))

        self.assert_error(
            response, INVALID_REQUEST, id=None, status=HTTPStatus.BAD_REQUEST
        )

    def test_params_not_object(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}

        response = self.post(message)

        self.assert_error(response, INVALID_REQUEST, status=HTTPStatus.BAD_REQUEST)

    def test_string_id_allowed(self):
        response = self.post(make_message("tools/list", id="abc"))

        self.assert_result(response, id="abc")


class MetadataValidationTests(ServerTestCase):
    def test_unsupported_protocol_version(self):
        response = self.post(
            make_message("tools/list", protocol_version="2027-01-01"),
            headers={"MCP-Protocol-Version": "2027-01-01"},
        )

        error = self.assert_error(
            response, UNSUPPORTED_PROTOCOL_VERSION, status=HTTPStatus.BAD_REQUEST
        )
        assert error["data"] == {
            "supported": mcp.server.supported_versions,
            "requested": "2027-01-01",
        }

    def test_missing_meta_protocol_version(self):
        response = self.post(make_message("tools/list", protocol_version=None))

        error = self.assert_error(
            response, INVALID_PARAMS, status=HTTPStatus.BAD_REQUEST
        )
        assert "protocolVersion" in error["message"]

    def test_missing_meta_entirely(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        response = self.post(message)

        self.assert_error(response, INVALID_PARAMS, status=HTTPStatus.BAD_REQUEST)

    def test_missing_client_capabilities(self):
        response = self.post(make_message("tools/list", client_capabilities=None))

        error = self.assert_error(
            response, INVALID_PARAMS, status=HTTPStatus.BAD_REQUEST
        )
        assert "clientCapabilities" in error["message"]

    def test_protocol_version_header_body_mismatch(self):
        response = self.post(make_message("tools/list", protocol_version="1900-01-01"))

        error = self.assert_error(
            response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST
        )
        assert "does not match body value" in error["message"]

    def test_missing_method_header(self):
        response = self.post(make_message("tools/list"), headers={"Mcp-Method": None})

        error = self.assert_error(
            response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST
        )
        assert "Mcp-Method" in error["message"]

    def test_method_header_mismatch(self):
        response = self.post(
            make_message("tools/list"), headers={"Mcp-Method": "tools/call"}
        )

        self.assert_error(response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST)


class DispatchTests(ServerTestCase):
    def test_unknown_method(self):
        response = self.post(make_message("resources/list"))

        error = self.assert_error(
            response, METHOD_NOT_FOUND, status=HTTPStatus.NOT_FOUND
        )
        assert "resources/list" in error["message"]

    def test_initialize_names_supported_versions(self):
        response = self.post(make_message("initialize"))

        error = self.assert_error(
            response, METHOD_NOT_FOUND, status=HTTPStatus.NOT_FOUND
        )
        assert PROTOCOL_VERSION in error["message"]


class DiscoverTests(ServerTestCase):
    def test_success(self):
        response = self.post(make_message("server/discover"))

        result = self.assert_result(response)
        assert result["supportedVersions"] == [
            PROTOCOL_VERSION,
            "2025-11-25",
            "2025-06-18",
            "2025-03-26",
        ]
        assert result["capabilities"] == {"tools": {}}
        assert result["instructions"] == (
            "Example MCP server used in the django-mcpz test suite."
        )
        assert result["ttlMs"] == 300_000
        assert result["cacheScope"] == "private"

    def test_defaults(self):
        response = self.client.post(
            "/secure-mcp",
            data=msgspec.json.encode(make_message("server/discover")),
            content_type="application/json",
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "server/discover",
                "Authorization": "Bearer test-token",
            },
        )

        result = response.json()["result"]
        assert "instructions" not in result


class ToolsListTests(ServerTestCase):
    def test_success(self):
        response = self.post(make_message("tools/list"))

        result = self.assert_result(response)
        assert result["ttlMs"] == 300_000
        assert result["cacheScope"] == "private"
        assert "nextCursor" not in result
        names = [tool["name"] for tool in result["tools"]]
        # Deterministic (registration) order.
        assert names == [
            "add",
            "greet",
            "unavailable",
            "crash",
            "create_widget",
            "noop",
            "unencodable",
            "regional",
            "multiply",
            "add_typed",
            "segment_length",
            "shout",
            "sig_noop",
        ]

        add = result["tools"][0]
        assert add["description"] == "Add two integers."
        assert add["inputSchema"]["required"] == ["a", "b"]
        assert add["outputSchema"]["required"] == ["sum"]

        greet = result["tools"][1]
        assert greet["title"] == "Greeter"
        assert greet["annotations"] == {"readOnlyHint": True}
        assert "outputSchema" not in greet

        regional = result["tools"][7]
        assert regional["icons"] == [
            {"src": "https://example.com/regional.png", "mimeType": "image/png"}
        ]

    def test_invalid_cursor(self):
        response = self.post(make_message("tools/list", {"cursor": "opaque"}))

        self.assert_error(response, INVALID_PARAMS)


class ToolsCallTests(ServerTestCase):
    def call(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.post(make_message("tools/call", params), **kwargs)

    def test_structured_result(self):
        response = self.call("add", {"a": 20, "b": 22})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["structuredContent"] == {"sum": 42}
        assert result["content"] == [{"type": "text", "text": '{"sum":42}'}]

    def test_text_result(self):
        response = self.call("greet", {"name": "Alice"})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "Hello, Alice!"}]
        assert "structuredContent" not in result

    def test_no_arguments(self):
        response = self.call("greet")

        result = self.assert_result(response)
        assert result["content"] == [{"type": "text", "text": "Hello, world!"}]

    def test_none_result(self):
        response = self.call("noop")

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["content"] == []
        assert "structuredContent" not in result

    def test_tool_error(self):
        response = self.call("unavailable")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "The flux capacitor is offline. Try the DeLorean instead.",
            }
        ]

    def test_unexpected_exception(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("crash")

        result = self.assert_result(response)
        assert result["isError"] is True
        # Internal details are not leaked.
        assert result["content"] == [
            {"type": "text", "text": "Tool 'crash' failed unexpectedly."}
        ]

    def test_unencodable_result(self):
        # A return value msgspec cannot encode is reported in-band too, not
        # as a 500 error.
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("unencodable")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Tool 'unencodable' failed unexpectedly."}
        ]

    def test_unknown_tool(self):
        response = self.call("does_not_exist")

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Unknown tool: 'does_not_exist'"

    def test_missing_name(self):
        response = self.post(make_message("tools/call", {"arguments": {}}))

        self.assert_error(response, INVALID_PARAMS)

    def test_arguments_not_object(self):
        message = make_message("tools/call", {"name": "add", "arguments": [1]})

        response = self.post(message)

        self.assert_error(response, INVALID_PARAMS)

    def test_missing_name_header(self):
        response = self.call("add", {"a": 1, "b": 2}, headers={"Mcp-Name": None})

        error = self.assert_error(
            response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST
        )
        assert "Mcp-Name" in error["message"]

    def test_name_header_mismatch(self):
        response = self.call("add", {"a": 1, "b": 2}, headers={"Mcp-Name": "greet"})

        self.assert_error(response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST)

    def test_name_header_base64(self):
        response = self.call(
            "add",
            {"a": 1, "b": 2},
            # "add" encoded with the Base64 sentinel format.
            headers={"Mcp-Name": "=?base64?YWRk?="},
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"sum": 3}

    def test_name_header_malformed_base64(self):
        response = self.call(
            "add", {"a": 1, "b": 2}, headers={"Mcp-Name": "=?base64?!!!?="}
        )

        self.assert_error(response, HEADER_MISMATCH, status=HTTPStatus.BAD_REQUEST)


class TypedSchemaTests(ServerTestCase):
    def call(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return self.post(
            make_message("tools/call", {"name": name, "arguments": arguments}),
            **kwargs,
        )

    def test_generated_schemas_in_list(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        multiply = tools["multiply"]
        assert multiply["inputSchema"] == {
            "title": "MultiplyParams",
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "The first factor."},
                "b": {"type": "integer", "default": 2},
            },
            "required": ["a"],
            "additionalProperties": False,
        }
        assert multiply["outputSchema"] == {
            "title": "MultiplyResult",
            "type": "object",
            "properties": {"product": {"type": "integer"}},
            "required": ["product"],
        }

    def test_nested_struct_schema_keeps_defs(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        schema = tools["segment_length"]["inputSchema"]
        assert schema["properties"]["start"] == {"$ref": "#/$defs/Point"}
        assert schema["$defs"]["Point"]["required"] == ["x", "y"]

    def test_call(self):
        response = self.call("multiply", {"a": 6, "b": 7})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["structuredContent"] == {"product": 42}
        assert result["content"] == [{"type": "text", "text": '{"product":42}'}]

    def test_call_default_applied(self):
        response = self.call("multiply", {"a": 5})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"product": 10}

    def test_invalid_arguments_wrong_type(self):
        response = self.call("multiply", {"a": "six"})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "Invalid arguments: Expected `int`, got `str` - at `$.a`",
            }
        ]

    def test_invalid_arguments_missing_field(self):
        response = self.call("multiply", {})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert "missing required field `a`" in result["content"][0]["text"]

    def test_invalid_arguments_unknown_field(self):
        response = self.call("multiply", {"a": 1, "c": 2})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Invalid arguments: unknown field `c`"}
        ]

    def test_invalid_arguments_unknown_fields(self):
        response = self.call("multiply", {"a": 1, "c": 2, "d": 3})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Invalid arguments: unknown fields `c`, `d`"}
        ]

    def test_forbid_unknown_fields_call(self):
        response = self.call("add_typed", {"a": 20, "b": 22})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"sum": 42}

    def test_forbid_unknown_fields_still_enforced(self):
        # A Struct that itself forbids unknown fields: msgspec enforces it,
        # with its own message.
        response = self.call("add_typed", {"a": 1, "b": 2, "c": 3})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "Invalid arguments: Object contains unknown field `c`",
            }
        ]

    def test_nested_struct_call(self):
        response = self.call(
            "segment_length",
            {"start": {"x": 0, "y": 0}, "end": {"x": 3, "y": 4}},
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"label": "", "length": 7}


class IconTests(SimpleTestCase):
    def test_path(self):
        request = RequestFactory().post("/mcp")

        icon = Icon(path="/mcp/icons/search.png", sizes=("48x48",)).resolve(request)

        assert icon == {
            "src": "http://testserver/mcp/icons/search.png",
            "mimeType": "image/png",
            "sizes": ["48x48"],
        }

    def test_static_and_path_exclusive(self):
        with pytest.raises(ImproperlyConfigured, match="exactly one"):
            Icon(static="a.png", path="/a.png")
        with pytest.raises(ImproperlyConfigured, match="exactly one"):
            Icon()

    def test_unguessable_mime_type_omitted(self):
        request = RequestFactory().get("/")
        icon = Icon(static="diner/logo.mystery")

        assert icon.resolve(request) == {
            "src": "http://testserver/static/diner/logo.mystery"
        }


class SignatureSchemaTests(ServerTestCase):
    def call(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return self.post(
            make_message("tools/call", {"name": name, "arguments": arguments}),
            **kwargs,
        )

    def test_generated_definition_in_list(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        shout = tools["shout"]
        assert shout["inputSchema"] == {
            "title": "ShoutParams",
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to shout."},
                "times": {"type": "integer", "default": 1},
            },
            "required": ["message"],
            "additionalProperties": False,
        }
        # Inferred from the return annotation.
        assert shout["outputSchema"] == {
            "title": "ShoutResult",
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        assert shout["annotations"] == {
            "readOnlyHint": True,
            "idempotentHint": True,
        }
        assert shout["icons"] == [
            {
                "src": "http://testserver/static/diner/shout.png",
                "mimeType": "image/png",
                "sizes": ["48x48"],
            },
            {
                "src": "http://testserver/static/diner/shout-dark.svg",
                "mimeType": "image/svg+xml",
                "theme": "dark",
            },
            {"src": "data:image/png;base64,AAAA", "mimeType": "image/png"},
        ]

    def test_no_parameters_definition(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert tools["sig_noop"]["inputSchema"] == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        # A None return annotation infers no output schema.
        assert "outputSchema" not in tools["sig_noop"]
        assert "annotations" not in tools["sig_noop"]

    def test_hint_annotations(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert tools["crash"]["annotations"] == {
            "destructiveHint": True,
            "openWorldHint": False,
        }

    def test_call(self):
        response = self.call("shout", {"message": "order up", "times": 2})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"text": "ORDER UP! ORDER UP!"}

    def test_call_default_applied(self):
        response = self.call("shout", {"message": "quiet"})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"text": "QUIET!"}

    def test_call_no_parameters(self):
        response = self.call("sig_noop", {})

        result = self.assert_result(response)
        assert result["content"] == []

    def test_invalid_arguments(self):
        response = self.call("shout", {"message": 5})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "Invalid arguments: Expected `str`, got `int` - at `$.message`",
            }
        ]

    def test_unknown_argument(self):
        response = self.call("sig_noop", {"volume": 11})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Invalid arguments: unknown field `volume`"}
        ]


class AuthTests(ServerTestCase):
    url = "/secure-mcp"

    def test_unauthorized(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_authorized(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == HTTPStatus.OK
        tools = response.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["secret_word"]


class CallLoggingTests(ServerTestCase):
    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        with self.assertLogs("django_mcpz.calls", level="INFO") as logs:
            self.post(
                make_message("tools/call", {"name": name, "arguments": arguments})
            )
        (record,) = logs.records
        return record

    def test_ok(self):
        record = self.call("add", {"a": 1, "b": 2})

        assert record.getMessage().startswith("Tool 'add': ok in ")
        assert record.getMessage().endswith("ms")
        assert record.server is mcp.server
        assert record.request.path == "/mcp"
        assert record.tool == "add"
        assert record.arguments == {"a": 1, "b": 2}
        assert record.outcome == "ok"
        assert record.duration >= 0.0

    def test_unknown_field(self):
        record = self.call("add_typed", {"a": 1, "b": 2, "c": 3})

        assert record.outcome == "invalid_arguments"

    def test_validation_error(self):
        record = self.call("add_typed", {"a": "one", "b": 2})

        assert record.outcome == "invalid_arguments"

    def test_tool_error(self):
        record = self.call("unavailable", {})

        assert record.outcome == "tool_error"

    def test_exception(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            record = self.call("crash", {})

        assert record.outcome == "exception"

    def test_not_logged_for_protocol_errors(self):
        # Unknown tools and header mismatches are not tool calls.
        with self.assertNoLogs("django_mcpz.calls", level="INFO"):
            response = self.post(
                make_message("tools/call", {"name": "nope", "arguments": {}})
            )

        self.assert_error(response, INVALID_PARAMS)


class RegistrationTests(SimpleTestCase):
    def make_server(self) -> MCPServer:
        return MCPServer(name="test", version="1.0.0", auth=public)

    def test_auth_required(self):
        with pytest.raises(TypeError, match="auth"):
            MCPServer(name="test", version="1.0.0")  # type: ignore[call-arg]

    def test_auth_invalid(self):
        with pytest.raises(ImproperlyConfigured, match="django_mcpz.server.public"):
            MCPServer(name="test", version="1.0.0", auth=None)  # type: ignore[arg-type]

    def test_auth_string_invalid(self):
        with pytest.raises(ImproperlyConfigured, match="not 'public'"):
            MCPServer(name="test", version="1.0.0", auth="public")  # type: ignore[arg-type]

    def test_public(self):
        assert public(RequestFactory().post("/mcp")) is None

    def test_invalid_minimum_protocol_version(self):
        with pytest.raises(ImproperlyConfigured, match="2025-03-26, 2026-07-28"):
            MCPServer(
                name="test",
                version="1.0.0",
                auth=public,
                minimum_protocol_version="2025-11-25",  # type: ignore[arg-type]
            )

    def test_header_annotation_rejected(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="x-mcp-header"):

            @server.tool(
                description="Routed.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "properties": {
                                "region": {"type": "string", "x-mcp-header": "Region"}
                            },
                        }
                    },
                },
            )
            def routed(request, arguments):  # pragma: no cover
                return None

    def test_header_annotation_from_meta_rejected(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="x-mcp-header"):

            @server.tool(description="Routed.")
            def routed(request: Any, params: Routed) -> None:  # pragma: no cover
                return None

    def test_duplicate_tool_name(self):
        server = self.make_server()

        @server.tool(description="One.", input_schema={"type": "object"})
        def something(request, arguments):  # pragma: no cover
            return None

        with pytest.raises(ImproperlyConfigured, match="already registered"):

            @server.tool(
                name="something", description="Two.", input_schema={"type": "object"}
            )
            def other(request, arguments):  # pragma: no cover
                return None

    def test_typeddict_input_schema(self):
        server = self.make_server()

        class EchoParams(TypedDict):
            text: str

        @server.tool(description="Echo.", input_schema=EchoParams)
        def echo(request, params):  # pragma: no cover
            return params["text"]

        tool = server._tools["echo"]
        assert tool.input_type is EchoParams
        assert tool.definition["inputSchema"] == {
            "title": "EchoParams",
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        assert tool.known_keys == frozenset({"text"})

    def test_recursive_struct_not_inlined(self):
        server = self.make_server()

        @server.tool(description="Tree.", input_schema=Node)
        def tree(request, params):  # pragma: no cover
            return None

        schema = server._tools["tree"].definition["inputSchema"]
        assert schema["$ref"] == "#/$defs/Node"
        assert "Node" in schema["$defs"]

    def test_non_ref_type_schema(self):
        server = self.make_server()

        @server.tool(description="Counts.", input_schema=dict[str, int])
        def counts(request, params):  # pragma: no cover
            return None

        assert server._tools["counts"].definition["inputSchema"] == {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        }

    def test_signature_zero_parameters(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="must accept the request"):

            @server.tool(description="Bad.")
            def bad():  # pragma: no cover
                return None

    def test_signature_missing_annotation(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="requires a type annotation"):

            @server.tool(description="Bad.")
            def bad(request, message):  # pragma: no cover
                return None

    def test_signature_var_keyword(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="positionally"):

            @server.tool(description="Bad.")
            def bad(request, **kwargs):  # pragma: no cover
                return None

    def test_signature_keyword_only(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="positionally"):

            @server.tool(description="Bad.")
            def bad(request, *, params):  # pragma: no cover
                return None

    def test_signature_too_many_parameters(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="one params argument"):

            @server.tool(description="Bad.")
            def bad(request, a, b):  # pragma: no cover
                return None

    def test_signature_unresolvable_annotation(self):
        server = self.make_server()

        with pytest.raises(ImproperlyConfigured, match="Could not resolve"):

            @server.tool(description="Bad.")
            def bad(request: Any, message: Missing) -> None:  # pragma: no cover
                return None


class ToolTransactionTests(ServerTestCase, TestCase):
    """
    Inside a transaction, as under ATOMIC_REQUESTS, a tool that raises leaves
    no writes behind, and the transaction stays usable.
    """

    def call(self, then: str) -> Any:
        message = make_message(
            "tools/call", {"name": "create_widget", "arguments": {"then": then}}
        )
        return self.post(message)

    def test_success_kept(self):
        response = self.call("ok")

        result = self.assert_result(response)
        assert result["isError"] is False
        assert Widget.objects.count() == 1

    def test_tool_error_rolled_back(self):
        response = self.call("error")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert Widget.objects.count() == 0

    def test_exception_rolled_back(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("crash")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert Widget.objects.count() == 0


class ToolAutocommitTests(ServerTestCase, TransactionTestCase):
    """Outside any transaction, the view behaves as any Django view does."""

    def call(self, then: str) -> Any:
        message = make_message(
            "tools/call", {"name": "create_widget", "arguments": {"then": then}}
        )
        return self.post(message)

    def test_writes_kept_without_atomic_requests(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("crash")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert Widget.objects.count() == 1

    def test_writes_rolled_back_with_atomic_requests(self):
        with (
            mock.patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}),
            self.assertLogs("django_mcpz", level="ERROR"),
        ):
            response = self.call("crash")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert Widget.objects.count() == 0

    def test_success_kept_with_atomic_requests(self):
        with mock.patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}):
            response = self.call("ok")

        result = self.assert_result(response)
        assert result["isError"] is False
        assert Widget.objects.count() == 1
