"""
Background tasks, for Django's tasks framework (Django 6.0+).

Enqueue them from a scheduler, or run the equivalent management command
from cron on earlier Django versions.
"""

from __future__ import annotations

from django.tasks import task

from django_mcpz.oauth import cleanup


@task
def clear_expired() -> dict[str, int]:
    """
    Delete expired and revoked codes and tokens, and unused clients.

    Returns the number of rows deleted, by kind.
    """
    return cleanup.clear_expired()
