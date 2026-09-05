from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Hide development server warning
# https://docs.djangoproject.com/en/stable/ref/django-admin/#envvar-DJANGO_RUNSERVER_HIDE_WARNING
os.environ["DJANGO_RUNSERVER_HIDE_WARNING"] = "true"

BASE_DIR = Path(__file__).parent.parent

DEBUG = True

SECRET_KEY = "django-insecure-example-project-only"

# This example runs the development server on localhost, and its MCP
# server is unauthenticated (auth=public). Don’t expose it beyond your own
# machine. See the docs for adding authentication.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "pizzeria",
    "django_mcpz",
]

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "pizzeria.urls"

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

USE_TZ = True

WSGI_APPLICATION = "pizzeria.wsgi.application"
