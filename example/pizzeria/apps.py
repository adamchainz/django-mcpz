from __future__ import annotations

from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate


class PizzeriaConfig(AppConfig):
    name = "pizzeria"

    def ready(self) -> None:
        post_migrate.connect(sync_reader, sender=self)


def sync_reader(using: str, **kwargs: Any) -> None:
    """
    On PostgreSQL, after migrating, make the mcpizza_reader role, which the
    SQL tools connect as through the reader alias, able to read the report
    views, and nothing else.
    """
    from django_mcpz.sql import sync_sql_grants
    from pizzeria.mcp import REPORT_TABLES

    if "reader" in settings.DATABASES and using == "default":
        sync_sql_grants(tables=REPORT_TABLES, reader="reader", using=using)
