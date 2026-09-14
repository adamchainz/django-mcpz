"""
Client ID metadata documents (draft-ietf-oauth-client-id-metadata-document).

A client identifies itself with an HTTPS URL, and the authorization server
fetches a JSON document from it describing the client. Fetching a URL the
client chose is the one place this app makes outbound requests, so the
fetch is guarded: HTTPS only, public addresses only, resolved once and
connected to directly, no redirects, a deadline, and a size cap.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit

from django.utils import timezone

from django_mcpz.oauth.conf import is_secure_url
from django_mcpz.oauth.models import NAME_MAX_LENGTH, URL_MAX_LENGTH, Client

TIMEOUT_SECONDS = 5
MAX_BYTES = 16 * 1024
CACHE_LIFETIME_SECONDS = 60 * 60


class MetadataError(Exception):
    """The document could not be fetched or is invalid."""


def is_metadata_url(client_id: str) -> bool:
    """Whether the client ID has the shape of a metadata document URL."""
    parts = urlsplit(client_id)
    return (
        len(client_id) <= URL_MAX_LENGTH
        and parts.scheme == "https"
        and bool(parts.netloc)
        and parts.path not in ("", "/")
    )


def get_metadata_client(client_id: str) -> Client:
    """
    Return the Client for a metadata document URL, fetching if not cached.

    Raises MetadataError if the document cannot be fetched or is invalid.
    """
    now = timezone.now()
    client = Client.objects.filter(
        client_id=client_id, kind=Client.Kind.METADATA
    ).first()
    if (
        client is not None
        and client.fetched_at is not None
        and (now - client.fetched_at).total_seconds() < CACHE_LIFETIME_SECONDS
    ):
        return client
    try:
        document = fetch_document(client_id)
    except MetadataError:
        # A client whose document is temporarily unreachable keeps working
        # from the cached copy, so an outage at the client does not force
        # its users to reconnect.
        if client is not None:
            return client
        raise
    # Concurrent first requests for the same client both fetch, and
    # update_or_create lets the second one update rather than collide.
    client, _ = Client.objects.update_or_create(
        client_id=client_id,
        defaults={
            "kind": Client.Kind.METADATA,
            "name": document["client_name"][:NAME_MAX_LENGTH],
            "redirect_uris": document["redirect_uris"],
            "fetched_at": now,
        },
    )
    return client


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """
    An HTTPS connection to an address resolved and checked beforehand.

    Resolving the name again at connect time would let a host whose DNS
    answers change between lookups pass the check with a public address and
    then connect to a private one.
    """

    def __init__(
        self, host: str, address: str, *, context: ssl.SSLContext, **kwargs: Any
    ) -> None:
        super().__init__(host, context=context, **kwargs)
        self.address = address
        self.ssl_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def _fetch(
    hostname: str,
    port: int,
    address: str,
    path: str,
    *,
    context: ssl.SSLContext | None = None,
) -> bytes:
    """GET the path from the host at the address, without following redirects."""
    connection = PinnedHTTPSConnection(
        hostname,
        address,
        port=port,
        timeout=TIMEOUT_SECONDS,
        context=context or ssl.create_default_context(),
    )
    # The socket timeout applies to each operation, so a server that sends a
    # byte at a time could hold the request open for hours. A timer shuts
    # the connection at a wall-clock deadline instead.
    timed_out = threading.Event()

    def abort() -> None:
        timed_out.set()
        with contextlib.suppress(OSError, AttributeError):
            connection.sock.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(TIMEOUT_SECONDS, abort)
    timer.start()
    expired = False
    try:
        connection.request(
            "GET",
            path,
            # Uncompressed, so the size cap applies to the bytes received.
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
        response = connection.getresponse()
        if response.status != http.HTTPStatus.OK:
            raise MetadataError(
                f"Client metadata document responded with status {response.status}."
            )
        body = response.read(MAX_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        # Shutting the connection surfaces as an error or as a short read,
        # depending on where the server had got to. A socket timeout means
        # the deadline has passed as well, the two being the same length,
        # and either may win the race to end a stalled operation.
        expired = timed_out.is_set() or isinstance(exc, TimeoutError)
        if not expired:
            raise
        body = b""
    finally:
        timer.cancel()
        connection.close()
    if expired or timed_out.is_set():
        raise MetadataError(
            f"Client metadata document took over {TIMEOUT_SECONDS} seconds."
        )
    return body


def fetch_document(url: str) -> dict[str, Any]:
    """Fetch and validate a metadata document from its URL."""
    parts = urlsplit(url)
    if not is_metadata_url(url) or parts.fragment:
        raise MetadataError("client_id is not an HTTPS URL with a path.")
    hostname = parts.hostname
    assert hostname is not None  # ensured by is_metadata_url
    port = parts.port or 443
    address = resolve_public_address(hostname, port)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        body = _fetch(hostname, port, address, path)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise MetadataError(f"Could not fetch client metadata: {exc}") from exc
    if len(body) > MAX_BYTES:
        raise MetadataError("Client metadata document is too large.")
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise MetadataError("Client metadata document is not valid JSON.") from exc
    return validate_document(document, url)


def resolve_public_address(hostname: str, port: int) -> str:
    """
    Resolve the host, refusing private, loopback, and link-local addresses.

    Returns the address to connect to.
    """
    try:
        results = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise MetadataError(f"Could not resolve {hostname}.") from exc
    for _family, _type, _proto, _canonname, sockaddr in results:
        # A scope suffix, as on link-local IPv6 addresses, is not part of
        # the address.
        address = ipaddress.ip_address(str(sockaddr[0]).partition("%")[0])
        if not address.is_global:
            raise MetadataError(f"{hostname} resolves to a non-public address.")
    return str(results[0][4][0])


def validate_document(document: object, url: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise MetadataError("Client metadata document is not a JSON object.")
    if document.get("client_id") != url:
        raise MetadataError("Client metadata client_id does not match its URL.")
    name = document.get("client_name")
    if not isinstance(name, str) or not name:
        raise MetadataError("Client metadata needs a client_name.")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        raise MetadataError("Client metadata needs a list of redirect_uris.")
    for uri in uris:
        # The same rules as dynamic registration: the authorization endpoint
        # redirects to these, so they must be safe to send a code to.
        if (
            not isinstance(uri, str)
            or len(uri) > URL_MAX_LENGTH
            or not is_secure_url(uri)
            or urlsplit(uri).fragment
        ):
            raise MetadataError(
                f"Client metadata redirect_uris must be HTTPS, or HTTP on"
                f" localhost, without a fragment: {uri!r}."
            )
    method = document.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise MetadataError(
            f"Unsupported token_endpoint_auth_method {method!r}: only public"
            " clients are supported."
        )
    return document
