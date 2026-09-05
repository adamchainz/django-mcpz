from __future__ import annotations

import pytest
from django.test import SimpleTestCase

from django_mcpz.headers import decode_header_value, encode_header_value


class EncodeHeaderValueTests(SimpleTestCase):
    def test_plain_ascii(self):
        assert encode_header_value("get_order") == "get_order"

    def test_non_ascii_uses_sentinel(self):
        encoded = encode_header_value("Hello, 世界")

        assert encoded.startswith("=?base64?")
        assert encoded.endswith("?=")
        assert decode_header_value(encoded) == "Hello, 世界"

    def test_surrounding_whitespace_uses_sentinel(self):
        encoded = encode_header_value(" padded ")

        assert encoded.startswith("=?base64?")
        assert decode_header_value(encoded) == " padded "

    def test_sentinel_lookalike_uses_sentinel(self):
        value = "=?base64?not-really?="

        encoded = encode_header_value(value)

        assert encoded != value
        assert decode_header_value(encoded) == value


class DecodeHeaderValueTests(SimpleTestCase):
    def test_plain(self):
        assert decode_header_value("get_order") == "get_order"

    def test_malformed(self):
        with pytest.raises(ValueError):
            decode_header_value("=?base64?!!!?=")

    def test_not_utf8(self):
        # Valid Base64, but of a byte that is not valid UTF-8.
        with pytest.raises(ValueError):
            decode_header_value("=?base64?/w==?=")
