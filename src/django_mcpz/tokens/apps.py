from __future__ import annotations

from django.apps import AppConfig


class TokensConfig(AppConfig):
    name = "django_mcpz.tokens"
    label = "django_mcpz_tokens"
    verbose_name = "MCP tokens"
    default_auto_field = "django.db.models.BigAutoField"
