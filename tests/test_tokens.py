from __future__ import annotations

from django.test import SimpleTestCase

from django_mcpz import tokens


class GenerateTests(SimpleTestCase):
    def test_prefix(self):
        assert tokens.generate().startswith(tokens.PREFIX)

    def test_unique(self):
        assert tokens.generate() != tokens.generate()
