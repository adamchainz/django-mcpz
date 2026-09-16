from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import http.server
import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone
from unittest_parametrize import ParametrizedTestCase, param, parametrize

from django_mcpz.oauth import cimd
from django_mcpz.oauth.models import Client
from tests.oauth.utils import (
    METADATA_URL,
    PUBLIC_ADDRESS,
    REDIRECT_URI,
    mock_metadata_fetch,
)


@contextlib.contextmanager
def local_https_server(
    routes: dict[str, tuple[int, bytes]],
) -> Iterator[tuple[int, ssl.SSLContext, list[dict[str, str]]]]:
    """
    Serve fixed responses over TLS on a local port, for testing the fetcher.

    Yields the port, a client context that trusts the server's self-signed
    certificate for the name "localhost", and a list that fills with the
    headers of each request received. Three paths misbehave: "/slow" sends
    one byte of a longer body and then stalls, "/stall" sends nothing and
    stalls, and "/drop" closes the connection without responding.
    """
    received: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(dict(self.headers))
            if self.path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                time.sleep(0.5)
                return
            if self.path == "/stall":
                time.sleep(0.5)
                return
            if self.path == "/drop":
                self.close_connection = True
                return
            status, body = routes[self.path]
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/doc")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    with tempfile.TemporaryDirectory() as directory:
        cert = f"{directory}/cert.pem"
        key = f"{directory}/key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=cert)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, client_context, received
        finally:
            server.shutdown()
            server.server_close()


class MetadataDocumentTests(ParametrizedTestCase, TestCase):
    document = {
        "client_id": METADATA_URL,
        "client_name": "Metadata client",
        "redirect_uris": [REDIRECT_URI],
    }

    def test_is_metadata_url(self):
        assert cimd.is_metadata_url(METADATA_URL)
        assert not cimd.is_metadata_url("abc123")
        assert not cimd.is_metadata_url("http://client.example/client.json")
        assert not cimd.is_metadata_url("https://client.example")
        assert not cimd.is_metadata_url("https://client.example/")
        assert not cimd.is_metadata_url("https://client.example/" + "a" * 500)
        # Unbalanced brackets make urlsplit() raise, which must not leak.
        assert not cimd.is_metadata_url("https://[::1/client.json")

    def test_invalid_port(self):
        with pytest.raises(cimd.MetadataError, match="invalid port"):
            cimd.fetch_document("https://client.example:x/client.json")

    def test_fetch_and_cache(self):
        fetch = mock.Mock(return_value=json.dumps(self.document).encode())

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", fetch),
        ):
            client = cimd.get_metadata_client(METADATA_URL)
            again = cimd.get_metadata_client(METADATA_URL)

        assert client == again
        assert client.name == "Metadata client"
        assert client.redirect_uris == [REDIRECT_URI]
        assert client.fetched_at is not None
        fetch.assert_called_once_with(
            "client.example", 443, PUBLIC_ADDRESS, "/oauth/client.json"
        )

    def test_fetch_path_with_query(self):
        url = METADATA_URL + "?v=2"
        body = json.dumps({**self.document, "client_id": url}).encode()
        fetch = mock.Mock(return_value=body)

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", fetch),
        ):
            cimd.fetch_document(url)

        fetch.assert_called_once_with(
            "client.example", 443, PUBLIC_ADDRESS, "/oauth/client.json?v=2"
        )

    def test_stale_cache_kept_when_fetch_fails(self):
        stale = timezone.now() - dt.timedelta(hours=2)
        Client.objects.create(
            client_id=METADATA_URL,
            kind=Client.Kind.METADATA,
            name="Old name",
            redirect_uris=[REDIRECT_URI],
            fetched_at=stale,
        )

        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("down")),
        ):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "Old name"
        assert client.fetched_at == stale

    def test_no_cache_when_fetch_fails(self):
        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("down")),
            pytest.raises(cimd.MetadataError, match="Could not fetch"),
        ):
            cimd.get_metadata_client(METADATA_URL)

    def test_refetch_after_cache_lifetime(self):
        stale = timezone.now() - dt.timedelta(hours=2)
        Client.objects.create(
            client_id=METADATA_URL,
            kind=Client.Kind.METADATA,
            name="Old name",
            redirect_uris=[],
            fetched_at=stale,
        )

        with mock_metadata_fetch(self.document):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "Metadata client"
        assert client.fetched_at is not None
        assert client.fetched_at > stale

    def test_fragment_rejected(self):
        with pytest.raises(cimd.MetadataError, match="HTTPS URL with a path"):
            cimd.fetch_document(METADATA_URL + "#x")

    def test_fetch_error(self):
        with (
            mock.patch.object(
                cimd, "resolve_public_address", return_value=PUBLIC_ADDRESS
            ),
            mock.patch.object(cimd, "_fetch", side_effect=OSError("boom")),
            pytest.raises(cimd.MetadataError, match="Could not fetch"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_too_large(self):
        with (
            mock_metadata_fetch(None, body=b"x" * (cimd.MAX_BYTES + 1)),
            pytest.raises(cimd.MetadataError, match="too large"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_invalid_json(self):
        with (
            mock_metadata_fetch(None, body=b"nope"),
            pytest.raises(cimd.MetadataError, match="not valid JSON"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_not_object(self):
        with (
            mock_metadata_fetch([]),
            pytest.raises(cimd.MetadataError, match="not a JSON object"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_client_id_mismatch(self):
        with (
            mock_metadata_fetch({**self.document, "client_id": "https://x.example/y"}),
            pytest.raises(cimd.MetadataError, match="does not match"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_missing_name(self):
        with (
            mock_metadata_fetch({**self.document, "client_name": ""}),
            pytest.raises(cimd.MetadataError, match="client_name"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_missing_redirect_uris(self):
        with (
            mock_metadata_fetch({**self.document, "redirect_uris": []}),
            pytest.raises(cimd.MetadataError, match="list of redirect_uris"),
        ):
            cimd.fetch_document(METADATA_URL)

    @parametrize(
        "uris",
        [
            param([1], id="non_string"),
            param(["http://client.example/cb"], id="mismatched_client"),
            param(["myapp://callback"], id="non_http"),
            param(["https://client.example/cb#frag"], id="fragment"),
            param(["https://client.example/" + "a" * 500], id="too_long"),
        ],
    )
    def test_bad_redirect_uris(self, uris):
        with (
            mock_metadata_fetch({**self.document, "redirect_uris": uris}),
            pytest.raises(cimd.MetadataError, match="redirect_uris"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_long_name_truncated(self):
        with mock_metadata_fetch({**self.document, "client_name": "n" * 300}):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.name == "n" * 200

    @parametrize(
        "method",
        [
            param("private_key_jwt", id="legacy_preference"),
            param(None, id="plural_only"),
        ],
    )
    def test_supported_public_auth(self, method):
        document = {
            **self.document,
            "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        }
        if method is not None:
            document["token_endpoint_auth_method"] = method
        with mock_metadata_fetch(document):
            client = cimd.get_metadata_client(METADATA_URL)

        assert client.client_id == METADATA_URL
        assert client.redirect_uris == self.document["redirect_uris"]

    @parametrize(
        "methods",
        [
            param(["private_key_jwt"], id="confidential_only"),
            param([], id="empty"),
            param("none", id="string"),
            param({"none": True}, id="object"),
            param(None, id="null"),
            param(["none", 1], id="non_string_member"),
        ],
    )
    def test_unsupported_auth_methods(self, methods):
        with (
            mock_metadata_fetch(
                {
                    **self.document,
                    "token_endpoint_auth_method": "none",
                    "token_endpoint_auth_methods_supported": methods,
                }
            ),
            pytest.raises(cimd.MetadataError, match="public clients"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_confidential_client(self):
        with (
            mock_metadata_fetch(
                {**self.document, "token_endpoint_auth_method": "private_key_jwt"}
            ),
            pytest.raises(cimd.MetadataError, match="public clients"),
        ):
            cimd.fetch_document(METADATA_URL)

    def test_fetch_over_tls(self):
        routes = {"/doc": (200, b"{}"), "/moved": (302, b""), "/missing": (404, b"")}

        with local_https_server(routes) as (port, context, received):
            body = cimd._fetch("localhost", port, "127.0.0.1", "/doc", context=context)
            with pytest.raises(cimd.MetadataError, match="status 302"):
                cimd._fetch("localhost", port, "127.0.0.1", "/moved", context=context)
            with pytest.raises(cimd.MetadataError, match="status 404"):
                cimd._fetch("localhost", port, "127.0.0.1", "/missing", context=context)

        assert body == b"{}"
        assert received[0]["Accept"] == "application/json"
        assert received[0]["Accept-Encoding"] == "identity"

    def test_fetch_deadline(self):
        with (
            local_https_server({}) as (port, context, _),
            mock.patch.object(cimd, "TIMEOUT_SECONDS", 0.2),
        ):
            # A body that stops arriving is cut short.
            with pytest.raises(cimd.MetadataError, match="took over 0.2 seconds"):
                cimd._fetch("localhost", port, "127.0.0.1", "/slow", context=context)
            # A response that never starts is abandoned.
            with pytest.raises(cimd.MetadataError, match="took over 0.2 seconds"):
                cimd._fetch("localhost", port, "127.0.0.1", "/stall", context=context)

    def test_fetch_dropped(self):
        # Other connection errors pass through, for fetch_document to report.
        with (
            local_https_server({}) as (port, context, _),
            pytest.raises(http.client.RemoteDisconnected),
        ):
            cimd._fetch("localhost", port, "127.0.0.1", "/drop", context=context)

    def test_resolve_public_address(self):
        # Real public addresses, since the documentation ranges are refused.
        results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            ),
        ]

        with mock.patch.object(socket, "getaddrinfo", return_value=results):
            address = cimd.resolve_public_address("client.example", 443)

        assert address == "1.1.1.1"

    def test_resolve_public_address_scoped(self):
        results = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 443, 0, 2))
        ]

        with (
            mock.patch.object(socket, "getaddrinfo", return_value=results),
            pytest.raises(cimd.MetadataError, match="non-public"),
        ):
            cimd.resolve_public_address("client.example", 443)

    def test_resolve_public_address_private(self):
        results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

        with (
            mock.patch.object(socket, "getaddrinfo", return_value=results),
            pytest.raises(cimd.MetadataError, match="non-public"),
        ):
            cimd.resolve_public_address("client.example", 443)

    def test_resolve_public_address_unresolvable(self):
        with (
            mock.patch.object(socket, "getaddrinfo", side_effect=OSError),
            pytest.raises(cimd.MetadataError, match="Could not resolve"),
        ):
            cimd.resolve_public_address("client.example", 443)
