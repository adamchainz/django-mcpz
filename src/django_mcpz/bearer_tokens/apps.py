from __future__ import annotations

from django.apps import AppConfig


class BearerTokensConfig(AppConfig):
    name = "django_mcpz.bearer_tokens"
    label = "django_mcpz_bearer_tokens"
    verbose_name = "MCP bearer tokens"
    default_auto_field = "django.db.models.BigAutoField"
