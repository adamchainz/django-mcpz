from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from django_mcpz.oauth.models import AccessToken, Client, RefreshToken

if TYPE_CHECKING:
    ClientModelAdmin = admin.ModelAdmin[Client]
    TokenModelAdmin = admin.ModelAdmin[Any]
else:
    ClientModelAdmin = TokenModelAdmin = admin.ModelAdmin


@admin.register(Client)
class ClientAdmin(ClientModelAdmin):
    list_display = ["name", "client_id", "kind", "created_at", "last_used_at"]
    list_filter = ["kind"]
    search_fields = ["name", "client_id"]
    readonly_fields = ["client_id", "kind", "created_at", "fetched_at", "last_used_at"]
    fields = [
        "name",
        "client_id",
        "kind",
        "redirect_uris",
        "created_at",
        "fetched_at",
        "last_used_at",
    ]

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Clients arrive through registration or metadata documents.
        return False


@admin.register(AccessToken)
class TokenAdmin(TokenModelAdmin):
    list_display = ["client", "user", "created_at", "expires_at", "revoked_at"]
    list_filter = [("revoked_at", admin.EmptyFieldListFilter)]
    readonly_fields = [
        "client",
        "user",
        "resource",
        "scope",
        "created_at",
        "expires_at",
        "revoked_at",
    ]
    fields = [
        "client",
        "user",
        "resource",
        "scope",
        "created_at",
        "expires_at",
        "revoked_at",
    ]
    actions = ["revoke"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.action(description="Revoke selected tokens")
    def revoke(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        count = queryset.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        messages.success(request, f"Revoked {count} token(s).")


@admin.register(RefreshToken)
class RefreshTokenAdmin(TokenAdmin):
    readonly_fields = TokenAdmin.readonly_fields + ["access_token", "used_at"]
    fields = TokenAdmin.fields + ["access_token", "used_at"]
