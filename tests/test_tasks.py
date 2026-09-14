from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import TestCase

from django_mcpz.bearer_tokens.models import Token
from tests.test_oauth import make_access_token

pytest.importorskip("django.tasks", reason="Django tasks framework, 6.0+")

from django_mcpz.bearer_tokens import tasks as bearer_tokens_tasks  # noqa: E402
from django_mcpz.oauth import tasks as oauth_tasks  # noqa: E402


class TasksTests(TestCase):
    """The cleanup tasks, run through the default immediate backend."""

    def test_bearer_tokens_clear_expired(self):
        user = User.objects.create_user("alice")
        Token.create(name="live", user=user)
        Token.create(
            name="expired",
            user=user,
            expires_at=dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        )

        result = bearer_tokens_tasks.clear_expired.enqueue()

        assert result.return_value == 1
        assert Token.objects.get().name == "live"

    def test_oauth_clear_expired(self):
        user = User.objects.create_user("alice")
        live, _ = make_access_token(user=user)
        make_access_token(user=user, lifetime=dt.timedelta(-1))

        result = oauth_tasks.clear_expired.enqueue()

        assert result.return_value == {
            "refresh_tokens": 0,
            "access_tokens": 1,
            "codes": 0,
            "clients": 0,
        }
        assert list(type(live).objects.all()) == [live]
