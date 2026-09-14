from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from django_mcpz import tokens
from django_mcpz.bearer_tokens.models import Token

if TYPE_CHECKING:
    ModelAdmin = admin.ModelAdmin[Token]
else:
    ModelAdmin = admin.ModelAdmin


@admin.register(Token)
class TokenAdmin(ModelAdmin):
    list_display = [
        "name",
        "user",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
    ]
    list_filter = [("revoked_at", admin.EmptyFieldListFilter)]
    search_fields = ["name"]
    raw_id_fields = ["user"]
    fields = [
        "name",
        "user",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
    ]
    # Revocation is one-way: it happens through the action, not the form.
    readonly_fields = ["created_at", "last_used_at", "revoked_at"]
    actions = ["revoke"]

    def save_model(
        self, request: HttpRequest, obj: Token, form: Any, change: bool
    ) -> None:
        if not change:
            value = tokens.generate()
            obj.digest = Token.digest_of(value)
            messages.success(
                request,
                f"Token created. Its value, shown only this once, is: {value}",
            )
        super().save_model(request, obj, form, change)

    @admin.action(description="Revoke selected tokens")
    def revoke(self, request: HttpRequest, queryset: QuerySet[Token]) -> None:
        count = queryset.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        messages.success(request, f"Revoked {count} token(s).")
