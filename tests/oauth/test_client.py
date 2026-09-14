from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx2
from django.contrib.auth.models import User
from django.test import LiveServerTestCase
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from django_mcpz.oauth import cimd
from django_mcpz.oauth.models import AccessToken
from django_mcpz.oauth.models import Client as OAuthClient

REDIRECT_URI = "http://localhost:3000/callback"
METADATA_URL = "https://client.example/oauth/client.json"


def run_async(coroutine: Any) -> Any:
    """
    Run the coroutine to completion in a worker thread.

    Running an event loop with real sockets in the test thread stops
    coverage tracing that thread on Python 3.11, so the loop gets a thread
    of its own.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


class MemoryStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class Browser:
    """
    Stand in for the user's browser during the authorization step.

    Visits the authorization URL the SDK produces with the user's session
    cookie, submits the consent form, and captures the redirect back to the
    client's callback URL.
    """

    def __init__(self, session_cookie: str) -> None:
        self.session_cookie = session_cookie
        self.authorization_url: str | None = None
        self.visits = 0

    async def redirect_handler(self, url: str) -> None:
        self.authorization_url = url

    async def callback_handler(self) -> AuthorizationCodeResult:
        assert self.authorization_url is not None
        self.visits += 1
        with httpx2.Client(cookies={"sessionid": self.session_cookie}) as http:
            page = http.get(self.authorization_url)
            assert page.status_code == 200, page.text
            assert "Allow access?" in page.text
            match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
            assert match is not None
            response = http.post(
                self.authorization_url,
                data={"csrfmiddlewaretoken": match[1], "decision": "allow"},
                headers={"Referer": self.authorization_url},
            )
        assert response.status_code == 302, response.text
        location = response.headers["Location"]
        assert location.startswith(REDIRECT_URI + "?")
        query = parse_qs(urlsplit(location).query)
        return AuthorizationCodeResult(
            code=query["code"][0], state=query["state"][0], iss=query["iss"][0]
        )


class OAuthClientTests(LiveServerTestCase):
    """
    The whole authorization flow, driven by the official MCP Python SDK
    client against a live server: discovery, registration, consent, code
    exchange, bearer calls, and refresh.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user("alice", password="pw")
        self.client.force_login(self.user)
        self.browser = Browser(self.client.cookies["sessionid"].value)
        self.storage = MemoryStorage()

    def provider(self, **kwargs: Any) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=f"{self.live_server_url}/oauth-mcp",
            client_metadata=OAuthClientMetadata(
                client_name="SDK test client",
                redirect_uris=[REDIRECT_URI],  # type: ignore[list-item]
                grant_types=["authorization_code", "refresh_token"],
                token_endpoint_auth_method="none",
            ),
            storage=self.storage,
            redirect_handler=self.browser.redirect_handler,
            callback_handler=self.browser.callback_handler,
            **kwargs,
        )

    async def whoami(self, provider: OAuthClientProvider | None = None) -> Any:
        if provider is None:
            provider = self.provider()
        async with httpx2.AsyncClient(auth=provider) as http:
            transport = streamable_http_client(
                f"{self.live_server_url}/oauth-mcp", http_client=http
            )
            async with Client(transport) as client:
                result = await client.call_tool("oauth_whoami", {})
                return result.structured_content

    def test_registration_flow(self):
        result = run_async(self.whoami())

        assert result == {"client": "SDK test client", "user": "alice"}
        assert self.browser.visits == 1
        registered = OAuthClient.objects.get()
        assert registered.kind == OAuthClient.Kind.REGISTERED
        assert registered.redirect_uris == [REDIRECT_URI]
        assert self.storage.tokens is not None
        assert self.storage.tokens.refresh_token is not None
        access = AccessToken.objects.get()
        assert access.user == self.user
        assert access.resource == f"{self.live_server_url}/oauth-mcp"

    def test_refresh(self):
        provider = self.provider()
        run_async(self.whoami(provider))
        first = AccessToken.objects.get()
        # The SDK refreshes when its own clock says the access token has
        # expired, so wind that clock forward.
        provider.context.token_expiry_time = 1.0

        result = run_async(self.whoami(provider))

        assert result == {"client": "SDK test client", "user": "alice"}
        assert self.browser.visits == 1, "Refreshed without asking the user again."
        first.refresh_from_db()
        assert first.is_valid, "Left to expire on its own."
        second = AccessToken.objects.exclude(pk=first.pk).get()
        assert second.is_valid
        assert second.code == first.code

    def test_metadata_document_flow(self):
        document = {
            "client_id": METADATA_URL,
            "client_name": "Metadata test client",
            "redirect_uris": [REDIRECT_URI],
        }

        with mock.patch.object(cimd, "fetch_document", return_value=document):
            result = run_async(
                self.whoami(self.provider(client_metadata_url=METADATA_URL))
            )

        assert result == {"client": "Metadata test client", "user": "alice"}
        registered = OAuthClient.objects.get()
        assert registered.kind == OAuthClient.Kind.METADATA
        assert registered.client_id == METADATA_URL
