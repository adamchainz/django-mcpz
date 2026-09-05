from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.urls import path

from pizzeria.mcp import server


def index(request: HttpRequest) -> HttpResponse:
    return HttpResponse(
        "MCPizza serves MCP at /mcp. See README.rst for how to connect.",
        content_type="text/plain; charset=utf-8",
    )


urlpatterns = [
    path("", index),
    path("mcp", server),
]
