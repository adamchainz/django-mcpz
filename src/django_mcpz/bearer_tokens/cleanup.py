from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from django_mcpz.bearer_tokens.models import Token


def clear_expired() -> int:
    """
    Delete expired and revoked bearer tokens.

    Returns the number deleted.
    """
    return Token.objects.filter(
        Q(expires_at__lt=timezone.now()) | Q(revoked_at__isnull=False)
    ).delete()[0]
