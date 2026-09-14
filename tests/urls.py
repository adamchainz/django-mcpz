from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

from tests.mcp import (
    bearer_tokens_server,
    oauth_server,
    perms_server,
    secure_server,
    server,
    strict_server,
)

urlpatterns = [
    path("mcp", server),
    path("perms-mcp", perms_server),
    path("secure-mcp", secure_server),
    path("strict-mcp", strict_server),
    path("bearer-tokens-mcp", bearer_tokens_server),
    path("oauth-mcp", oauth_server),
    path("admin/", admin.site.urls),
    path("oauth/", include("django_mcpz.oauth.urls")),
    path("", include("django_mcpz.oauth.wellknown")),
]
