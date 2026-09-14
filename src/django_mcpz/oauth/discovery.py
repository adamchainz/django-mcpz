"""
Where the authorization server and the MCP servers are, derived from the
URLconf and each request, so nothing needs configuring.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.http import HttpRequest
from django.urls import Resolver404, resolve, reverse

from django_mcpz.server import MCPServer, host_allowed


def canonical(url: str) -> str:
    """The URL with its scheme and host lowercased, for comparison."""
    parts = urlsplit(url)
    return parts._replace(
        scheme=parts.scheme.lower(), netloc=parts.netloc.lower()
    ).geturl()


def issuer_path() -> str:
    """
    The path the authorization server's URLs are included under.

    Empty when included at the site root, otherwise like "/oauth", with no
    trailing slash.
    """
    return reverse("django_mcpz_oauth:authorize").removesuffix("/authorize")


def issuer_for(request: HttpRequest) -> str:
    """The authorization server's URL, on the request's scheme and host."""
    return canonical(f"{request.scheme}://{request.get_host()}{issuer_path()}")


def mcp_server_at(path: str) -> MCPServer | None:
    """The MCPServer the path is routed to, if any."""
    try:
        match = resolve(path)
    except Resolver404:
        return None
    server = match.func
    return server if isinstance(server, MCPServer) else None


def resource_for(request: HttpRequest) -> str:
    """The resource URL of the MCP server handling the request."""
    return canonical(request.build_absolute_uri(request.path))


def validate_resource(resource: str) -> str | None:
    """
    The canonical form of a client-supplied resource URL, or None.

    Valid means an HTTP(S) URL on one of this site's allowed hosts, with no
    query string or fragment, whose path is routed to an MCPServer.
    """
    parts = urlsplit(resource)
    if (
        parts.scheme.lower() not in ("http", "https")
        or parts.hostname is None
        or not host_allowed(parts.hostname)
        or parts.query
        or parts.fragment
        or mcp_server_at(parts.path) is None
    ):
        return None
    return canonical(resource)


def resource_metadata_url(resource: str) -> str:
    """The RFC 9728 metadata URL for a resource, inserted after its host."""
    parts = urlsplit(resource)
    return (
        f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource"
        f"{parts.path}"
    )
