"""
Forbid framing the authorize page, with the Content-Security-Policy
frame-ancestors directive.

Django 6.0 added CSP support: a SECURE_CSP setting and a middleware that
builds the header from it, leaving alone any header a view set itself. Where
that middleware is in use, the page's policy is the site's own with
frame-ancestors overridden, so the rest of the site's policy still applies.
Otherwise the header carries the directive alone.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def frame_ancestors_none(
    view: Callable[..., HttpResponse],
) -> Callable[..., HttpResponse]:
    """Forbid framing the view's responses."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Django’s ContentSecurityPolicyMiddleware, or a subclass of it, marks
        # each request with a nonce.
        if hasattr(request, "_csp_nonce"):
            from django.utils.csp import CSP
            from django.views.decorators.csp import csp_override

            policy = {**(settings.SECURE_CSP or {}), "frame-ancestors": [CSP.NONE]}
            return csp_override(policy)(view)(request, *args, **kwargs)
        response = view(request, *args, **kwargs)
        response["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    return wrapper
