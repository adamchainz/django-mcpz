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
    "django.contrib.staticfiles",
]

# Static files, for the server's icon, are served by runserver in DEBUG mode.
STATIC_URL = "static/"

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "pizzeria.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    }
]

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Set MCPIZZA_POSTGRES=1 to use PostgreSQL instead, which also enables the
# SQL tools. They connect through the "reader" alias, as the mcpizza_reader
# role that migrations create, which may only read the report views.
if os.environ.get("MCPIZZA_POSTGRES"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "mcpizza",
            "HOST": os.environ.get("PGHOST", "localhost"),
            "USER": os.environ.get("PGUSER", ""),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
        },
        "reader": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "mcpizza",
            "HOST": os.environ.get("PGHOST", "localhost"),
            "USER": "mcpizza_reader",
            "PASSWORD": "mcpizza",
            # In tests, connect to the default alias's test database.
            "TEST": {"MIRROR": "default"},
        },
    }

USE_TZ = True

WSGI_APPLICATION = "pizzeria.wsgi.application"
