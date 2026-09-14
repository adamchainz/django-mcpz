from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import TestCase

from django_mcpz.bearer_tokens.models import Token

pytest.importorskip("django.tasks", reason="Django tasks framework, 6.0+")

from django_mcpz.bearer_tokens import tasks  # noqa: E402


class TasksTests(TestCase):
    """The cleanup task, run through the default immediate backend."""

    def test_clear_expired(self):
        user = User.objects.create_user("alice")
        Token.create(name="live", user=user)
        Token.create(
            name="expired",
            user=user,
            expires_at=dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        )

        result = tasks.clear_expired.enqueue()

        assert result.return_value == 1
        assert Token.objects.get().name == "live"
