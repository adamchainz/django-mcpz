from __future__ import annotations

from django.contrib import admin
from django.urls import path

from tests.example import (
    perms_server,
    secure_server,
    server,
    strict_server,
    tokens_server,
)

urlpatterns = [
    path("mcp", server),
    path("perms-mcp", perms_server),
    path("secure-mcp", secure_server),
    path("strict-mcp", strict_server),
    path("tokens-mcp", tokens_server),
    path("admin/", admin.site.urls),
]
