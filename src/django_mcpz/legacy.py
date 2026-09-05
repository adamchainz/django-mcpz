"""
Support for clients on the 2025 protocol revisions, which open with an
initialize handshake, served in the stateless server mode those revisions
allow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.http import HttpRequest, HttpResponse

from django_mcpz.jsonrpc import METHOD_NOT_FOUND, error_response, result_response

if TYPE_CHECKING:
    from django_mcpz.server import MCPServer

LEGACY_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]


def is_legacy_request(request: HttpRequest) -> bool:
    # 2025-06-18 and later clients send their negotiated version in the
    # header. 2025-03-26 clients send no header at all.
    header_version = request.headers.get("MCP-Protocol-Version")
    return header_version is None or header_version in LEGACY_PROTOCOL_VERSIONS


def legacy_dispatch(
    server: MCPServer,
    request: HttpRequest,
    request_id: str | int,
    method: str,
    params: dict[str, Any],
) -> HttpResponse:
    """
    Serve a client on a 2025 protocol revision.

    Those revisions allow a stateless server mode: initialize is answered
    without a session ID, and every request then stands alone, exactly
    as in 2026-07-28, minus its metadata and header requirements. Errors
    travel in HTTP 200 responses, as those transports expect.
    """
    if method == "initialize":
        requested = params.get("protocolVersion")
        if requested in LEGACY_PROTOCOL_VERSIONS:
            version = requested
        else:
            version = LEGACY_PROTOCOL_VERSIONS[0]
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": server.server_info,
        }
        if server.instructions is not None:
            result["instructions"] = server.instructions
        return result_response(request_id, result)
    elif method == "ping":
        return result_response(request_id, {})
    elif method == "server/discover":
        return server._discover(request_id)
    elif method == "tools/list":
        return server._tools_list(request, request_id, params)
    elif method == "tools/call":
        return server._tools_call(request, request_id, params, legacy=True)
    else:
        return error_response(
            request_id, METHOD_NOT_FOUND, f"Method not found: {method!r}"
        )
