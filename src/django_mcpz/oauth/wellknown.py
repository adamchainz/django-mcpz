"""
URL patterns for oauth discovery documents, which live under /.well-known/.

Include at the site root:

    path("", include("django_mcpz.oauth.wellknown")),

RFC 8414 puts the authorization server's document at
/.well-known/oauth-authorization-server followed by the issuer's path, and
RFC 9728 puts each MCP server's document at
/.well-known/oauth-protected-resource followed by the server's path, so
both are served for any path and checked against the URLconf.
"""

from __future__ import annotations

from django.urls import include, path

from django_mcpz.oauth import views

urlpatterns = [
    path(
        ".well-known/",
        include(
            [
                path(
                    "oauth-authorization-server",
                    views.authorization_server_metadata,
                ),
                path(
                    "oauth-authorization-server/<path:issuer_path>",
                    views.authorization_server_metadata,
                ),
                path(
                    "oauth-protected-resource",
                    views.protected_resource_metadata,
                ),
                path(
                    "oauth-protected-resource/<path:resource_path>",
                    views.protected_resource_metadata,
                ),
            ]
        ),
    ),
]
