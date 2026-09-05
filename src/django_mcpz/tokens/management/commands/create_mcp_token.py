from __future__ import annotations

import datetime as dt
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from django_mcpz.tokens.models import Token


class Command(BaseCommand):
    help = "Create an MCP token, printing its value once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "name", help="A name for the token, e.g. the client using it."
        )
        parser.add_argument(
            "--user",
            required=True,
            help="Username of the user the token acts as.",
        )
        parser.add_argument(
            "--expires-in-days",
            type=int,
            help="Make the token expire this many days from now.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        User = get_user_model()
        try:
            user = User._default_manager.get_by_natural_key(options["user"])
        except User.DoesNotExist:
            raise CommandError(f"No user named {options['user']!r}.") from None
        expires_at = None
        if options["expires_in_days"] is not None:
            if options["expires_in_days"] <= 0:
                raise CommandError("--expires-in-days must be positive.")
            expires_at = timezone.now() + dt.timedelta(days=options["expires_in_days"])
        _, value = Token.create(name=options["name"], user=user, expires_at=expires_at)
        self.stdout.write(value)
