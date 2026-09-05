from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.utils import timezone

# A generic prefix, so leaked tokens are recognisable by secret scanners
# without naming the server software.
TOKEN_PREFIX = "mcp_"


class Token(models.Model):
    """
    A bearer token identifying one MCP client, acting as a user.

    Only a SHA-256 digest is stored. The token itself is shown once, when
    created.
    """

    name = models.CharField(max_length=200)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mcp_tokens",
    )
    digest = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # How often last_used_at is written, so busy clients do not cost a
    # database write per request.
    LAST_USED_RESOLUTION = dt.timedelta(minutes=1)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def generate() -> str:
        """Generate a new token value, to be digested before storage."""
        return TOKEN_PREFIX + secrets.token_urlsafe(32)

    @staticmethod
    def digest_of(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        name: str,
        user: AbstractBaseUser,
        expires_at: dt.datetime | None = None,
    ) -> tuple[Token, str]:
        """
        Create a token, returning the instance and the token value.

        The value cannot be recovered later, so pass it on straight away.
        """
        value = cls.generate()
        token = cls.objects.create(
            name=name, user=user, expires_at=expires_at, digest=cls.digest_of(value)
        )
        return token, value

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def revoke(self) -> None:
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])

    def record_use(self) -> None:
        """
        Set last_used_at to now, unless it was set within LAST_USED_RESOLUTION.
        """
        now = timezone.now()
        if (
            self.last_used_at is not None
            and now - self.last_used_at < self.LAST_USED_RESOLUTION
        ):
            return
        self.last_used_at = now
        # Write directly rather than save(), so a concurrent revoke() or
        # admin edit is not overwritten.
        Token.objects.filter(pk=self.pk).update(last_used_at=now)
