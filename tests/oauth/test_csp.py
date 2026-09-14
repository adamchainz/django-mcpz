from __future__ import annotations

import django
import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from django_mcpz.oauth.csp import frame_ancestors_none

csp_support = pytest.mark.skipif(
    django.VERSION < (6, 0), reason="Django's CSP support arrived in 6.0."
)


@frame_ancestors_none
def view(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


class FrameAncestorsNoneTests(SimpleTestCase):
    def test_without_middleware(self):
        response = view(RequestFactory().get("/"))

        assert response["Content-Security-Policy"] == "frame-ancestors 'none'"

    @csp_support
    def test_with_middleware(self):
        from django.middleware.csp import ContentSecurityPolicyMiddleware

        policy = {"default-src": ["'self'"], "frame-ancestors": ["'self'"]}
        with override_settings(SECURE_CSP=policy):
            response = ContentSecurityPolicyMiddleware(view)(RequestFactory().get("/"))
        assert isinstance(response, HttpResponse)

        assert (
            response["Content-Security-Policy"]
            == "default-src 'self'; frame-ancestors 'none'"
        )

    @csp_support
    def test_with_middleware_and_no_policy(self):
        from django.middleware.csp import ContentSecurityPolicyMiddleware

        with override_settings(SECURE_CSP={}):
            response = ContentSecurityPolicyMiddleware(view)(RequestFactory().get("/"))
        assert isinstance(response, HttpResponse)

        assert response["Content-Security-Policy"] == "frame-ancestors 'none'"
