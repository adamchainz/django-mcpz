from __future__ import annotations

from typing import Any

from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import post_migrate


class TestsConfig(AppConfig):
    name = "tests"

    def ready(self) -> None:
        post_migrate.connect(sync_reader, sender=self)


def sync_reader(using: str, **kwargs: Any) -> None:
    from django_mcpz.sql import sync_sql_grants
    from tests.mcp import SQL_TABLES

    if connections[using].vendor == "postgresql":
        sync_sql_grants(tables=SQL_TABLES, reader="reader", using=using)
