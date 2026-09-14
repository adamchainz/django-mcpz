from __future__ import annotations

from django.test import SimpleTestCase

from django_mcpz.oauth import urls, wellknown


class URLsTests(SimpleTestCase):
    def test_endpoint_paths(self):
        paths = [str(pattern.pattern) for pattern in urls.urlpatterns]

        assert paths == ["authorize", "token", "register", "revoke"]

    def test_wellknown_paths(self):
        prefixes = [str(pattern.pattern) for pattern in wellknown.urlpatterns]
        assert prefixes == [".well-known/"]

        paths = [
            str(pattern.pattern) for pattern in wellknown.urlpatterns[0].url_patterns
        ]

        assert paths == [
            "oauth-authorization-server",
            "oauth-authorization-server/<path:issuer_path>",
            "oauth-protected-resource",
            "oauth-protected-resource/<path:resource_path>",
        ]
