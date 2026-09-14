"""
URL patterns for the authorization server's endpoints.

Include under any path. That path becomes the issuer's, so the endpoints
are served beneath it:

    path("oauth/", include("django_mcpz.oauth.urls")),
"""

from __future__ import annotations

from django.urls import path

from django_mcpz.oauth import views

app_name = "django_mcpz_oauth"

urlpatterns = [
    path("authorize", views.authorize, name="authorize"),
    path("token", views.token, name="token"),
    path("register", views.register, name="register"),
    path("revoke", views.revoke, name="revoke"),
]
