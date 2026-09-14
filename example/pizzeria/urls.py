from __future__ import annotations

import datetime as dt

from django.http import HttpRequest, HttpResponse
from django.urls import path, register_converter

from pizzeria import views
from pizzeria.mcp import server


class DateConverter:
    regex = r"\d{4}-\d{2}-\d{2}"

    def to_python(self, value: str) -> dt.date:
        # A ValueError, for an impossible date, means no match, so a 404.
        return dt.date.fromisoformat(value)

    def to_url(self, value: dt.date) -> str:
        return value.isoformat()


register_converter(DateConverter, "date")


def index(request: HttpRequest) -> HttpResponse:
    return HttpResponse(
        "MCPizza serves MCP at /mcp, and the menu at /menu/<date>/."
        " See README.rst for how to connect.",
        content_type="text/plain; charset=utf-8",
    )


urlpatterns = [
    path("", index),
    path("mcp", server),
    path("menu/<date:on_date>/", views.menu, name="menu"),
]
