from __future__ import annotations

import datetime as dt

from django.utils import timezone

from django_mcpz.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    RefreshToken,
    expired_or_revoked,
)

# Dynamically registered clients that never obtained a token are deleted
# after this long, since open registration would otherwise fill the table.
UNUSED_CLIENT_LIFETIME = dt.timedelta(days=1)


def clear_expired() -> dict[str, int]:
    """
    Delete expired and revoked codes and tokens, and unused clients.

    Returns the number of rows deleted, by kind.
    """
    now = timezone.now()
    # Deleted in dependency order. A refresh token cascades from its access
    # token, so an access token stays until its refresh token has expired or
    # been revoked. Codes stay while any token links to them, so that reuse
    # of a refresh token can still revoke the family.
    num_deleted_refresh_tokens = RefreshToken.objects.filter(
        expired_or_revoked()
    ).delete()[0]
    num_deleted_access_tokens = AccessToken.objects.filter(
        expired_or_revoked(), refresh_token__isnull=True
    ).delete()[0]
    num_deleted_codes = AuthorizationCode.objects.filter(
        expires_at__lt=now,
        accesstoken__isnull=True,
        refreshtoken__isnull=True,
    ).delete()[0]
    # Counted by model, since deleting a client cascades to any codes its
    # users consented to without completing.
    num_deleted_clients = (
        Client.objects.filter(
            kind=Client.Kind.REGISTERED,
            last_used_at__isnull=True,
            created_at__lt=now - UNUSED_CLIENT_LIFETIME,
        )
        .delete()[1]
        .get(Client._meta.label, 0)
    )
    return {
        "refresh_tokens": num_deleted_refresh_tokens,
        "access_tokens": num_deleted_access_tokens,
        "codes": num_deleted_codes,
        "clients": num_deleted_clients,
    }
