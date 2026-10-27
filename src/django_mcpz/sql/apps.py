from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate


class SQLConfig(AppConfig):
    name = "django_mcpz.sql"
    label = "django_mcpz_sql"
    verbose_name = "MCP SQL tools"

    def ready(self) -> None:
        from django_mcpz.sql.grants import sync_readers

        post_migrate.connect(sync_readers, sender=self)
