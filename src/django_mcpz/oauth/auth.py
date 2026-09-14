from __future__ import annotations

from http import HTTPStatus

from django.http import HttpRequest, HttpResponse

from django_mcpz.oauth.discovery import resource_for, resource_metadata_url
from django_mcpz.oauth.models import AccessToken
from django_mcpz.tokens import sha256_hex


def oauth_auth(request: HttpRequest) -> HttpResponse | None:
    """
    Authenticate with an access token from the django_mcpz.oauth app.

    Requires the Authorization header to carry a valid access token, issued
    for this MCP server, as a bearer credential. On success, attaches the
    AccessToken as request.mcp_token and sets request.user to its user.
    Otherwise responds 401 with the challenge that points clients at the
    authorization server.
    """
    resource = resource_for(request)
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return _challenge(resource)
    token = (
        AccessToken.objects.select_related("user")
        .filter(digest=sha256_hex(credential))
        .first()
    )
    if (
        token is None
        or not token.is_valid
        or token.resource != resource
        or not getattr(token.user, "is_active", True)
    ):
        return _challenge(resource, error="invalid_token")
    request.mcp_token = token  # type: ignore[attr-defined]
    request.user = token.user
    return None


def _challenge(resource: str, error: str | None = None) -> HttpResponse:
    parts = [f'resource_metadata="{resource_metadata_url(resource)}"']
    if error is not None:
        parts.insert(0, f'error="{error}"')
    return HttpResponse(
        status=HTTPStatus.UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)},
    )
