from __future__ import annotations

from django.apps import AppConfig


class OAuthConfig(AppConfig):
    name = "django_mcpz.oauth"
    label = "django_mcpz_oauth"
    verbose_name = "MCP OAuth"
    default_auto_field = "django.db.models.BigAutoField"
