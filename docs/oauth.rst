.. _server-oauth:

OAuth
=====

.. currentmodule:: django_mcpz.oauth

Hosted assistants such as Claude.ai and ChatGPT connect to MCP servers through OAuth: the user clicks “connect”, logs in to your site, approves access, and the assistant receives tokens to call your server with.
The optional ``django_mcpz.oauth`` app provides that, as an authorization server built into your project, plus an ``auth`` callable for your MCP servers.
It implements the `MCP authorization specification <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>`__ and the standards it draws on, in a deliberately narrow profile: the OAuth 2.1 authorization code grant with PKCE, refresh tokens, public clients only, client ID metadata documents and dynamic registration, resource indicators, and revocation.

How it works
------------

When an assistant first calls your MCP server without a token, it receives a 401 response pointing at a small JSON document describing the server.
That document names your authorization server, whose own JSON document lists its endpoints, and gives the MCP server’s title, or name, for the assistant to show.
The assistant then identifies itself as an OAuth client, sends the user’s browser to your site to log in and approve access, and exchanges the resulting code for an access token and a refresh token.
From then on it calls your MCP server with ``Authorization: Bearer ...`` on every request, exactly as a :doc:`bearer tokens <bearer_tokens>` client does, and refreshes the access token when it expires.

Tokens are opaque random strings, stored as SHA-256 digests, so a leaked database does not reveal usable credentials.
Access tokens last one hour by default and refresh tokens thirty days, after which the user reconnects.
Each token is bound to the MCP server URL it was issued for, scheme and host included, and rejected anywhere else.

Assistants identify themselves in one of two ways.
Some register through the registration endpoint, receiving a random client ID.
Others use a client ID metadata document: the client ID is an HTTPS URL, from which your server fetches a JSON document describing the client, at most once an hour per client.
That fetch is the one outbound request the app makes, and it is made only from the authorize page, for logged-in users, to public addresses, without following redirects.

Setup
-----

1. Add the app to ``INSTALLED_APPS`` and run ``migrate``.
   It needs |django.contrib.auth|__ and |django.contrib.sessions|__, which most projects have:

   .. |django.contrib.auth| replace:: ``django.contrib.auth``
   __ https://docs.djangoproject.com/en/stable/ref/contrib/auth/
   .. |django.contrib.sessions| replace:: ``django.contrib.sessions``
   __ https://docs.djangoproject.com/en/stable/topics/http/sessions/

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_mcpz",
           "django_mcpz.oauth",
           ...,
       ]

2. Include the app’s URLs.
   The endpoints go under a path of your choosing, which becomes the authorization server’s URL, and the two discovery documents go at the site root, since they live under ``/.well-known/``:

   .. code-block:: python

       from django.urls import include, path

       from example.mcp import server

       urlpatterns = [
           path("mcp", server),
           path("oauth/", include("django_mcpz.oauth.urls")),
           path("", include("django_mcpz.oauth.wellknown")),
       ]

   This serves ``/oauth/authorize``, ``/oauth/token``, ``/oauth/register``, and ``/oauth/revoke``, plus the discovery documents at ``/.well-known/oauth-authorization-server/oauth`` and ``/.well-known/oauth-protected-resource/mcp``.
   Those last two paths follow from the endpoints’ path and the MCP server’s path, per the RFCs, so clients can find them.
   Nothing needs configuring: the app reads the paths from the URLconf, and the scheme and host from each request, as |build_absolute_uri()|__ does.

   .. |build_absolute_uri()| replace:: ``build_absolute_uri()``
   __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpRequest.build_absolute_uri

3. Serve the site over HTTPS, with |ALLOWED_HOSTS|__ set.
   OAuth requires HTTPS everywhere apart from ``localhost``, and the host is what tokens are bound to, so a request on an unexpected host must be refused, which ``ALLOWED_HOSTS`` does.
   Behind a proxy that terminates TLS, set |SECURE_PROXY_SSL_HEADER|__ so Django knows the requests were HTTPS.

   .. |ALLOWED_HOSTS| replace:: ``ALLOWED_HOSTS``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts
   .. |SECURE_PROXY_SSL_HEADER| replace:: ``SECURE_PROXY_SSL_HEADER``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#secure-proxy-ssl-header

4. Pass the app’s authentication callable to your server:

   .. code-block:: python

       from django_mcpz.oauth.auth import oauth_auth
       from django_mcpz.server import MCPServer

       server = MCPServer(name="shop", version="1.0.0", auth=oauth_auth)

5. Make sure users can log in.
   The authorize page sends anonymous users to |LOGIN_URL|__, so any login flow works, including social login.
   Django’s |LoginView|__ is enough if you have nothing else.

   .. |LOGIN_URL| replace:: ``LOGIN_URL``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#login-url
   .. |LoginView| replace:: ``LoginView``
   __ https://docs.djangoproject.com/en/stable/topics/auth/default/#django.contrib.auth.views.LoginView

6. Schedule the cleanup of expired codes and tokens, as covered in :ref:`cleanup`.

Then add your server’s URL to the assistant as a connector, and it will take you through the login and consent pages.

The consent page
----------------

Every authorization shows a page naming the client, the MCP server, and the user, with Allow and Deny buttons.
It is shown every time, even for a client the user approved before, since anyone can register a client with any name, and the page is what lets the user notice an impostor.
Requests that cannot be honoured, such as one from an unknown client, show an error page instead.

The two pages are rendered from three templates:

``django_mcpz/oauth/base.html``
    The base HTML document for both pages, with ``title`` and ``content`` blocks, a link to the app’s ``django_mcpz/oauth/base.css`` static file, a ``color-scheme`` meta tag so the pages follow the user’s light or dark colour scheme, and a ``noindex`` robots tag.

``django_mcpz/oauth/authorize.html``
    The consent page.
    Its context holds ``user``, ``client``, ``resource``, ``scope``, ``redirect_uri``, ``redirect_host``, and ``redirect_is_local``, the last being whether the client is redirecting to ``localhost``.
    It must keep a form that posts a ``decision`` field of ``allow`` or ``deny``, with a CSRF token.

``django_mcpz/oauth/error.html``
    The error page, with ``error`` and ``description`` in its context.

Override them as you would any app’s templates, as covered in |Overriding templates|__: put files of the same names in a directory listed in your ``TEMPLATES`` setting’s ``DIRS``, or in an app listed before ``django_mcpz.oauth`` in ``INSTALLED_APPS``.
To restyle both pages at once, override only the base template, for example with one that extends your site’s own base template and fills in its blocks.
The pages are served with a ``Content-Security-Policy`` header of ``frame-ancestors 'none'``, so they cannot be shown inside a frame on another page.

.. |Overriding templates| replace:: Overriding templates
__ https://docs.djangoproject.com/en/stable/howto/overriding-templates/

Serving developer tools too
---------------------------

Developer tools like Claude Code send a pasted bearer token rather than going through OAuth, as covered in :ref:`server-bearer-tokens-clients`.
To serve both kinds of client from one server, write an ``auth`` callable that accepts a token from either app, returning the OAuth challenge when neither matches:

.. code-block:: python

    from django_mcpz.oauth.auth import oauth_auth
    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth


    def either_auth(request):
        if token_auth(request) is None:
            return None
        return oauth_auth(request)


    server = MCPServer(name="shop", version="1.0.0", auth=either_auth)

Managing clients and tokens
---------------------------

The admin lists clients, access tokens, and refresh tokens, with an action to revoke tokens.
Revoking a user’s refresh token cuts the assistant off at the next refresh.
Deactivating the user cuts it off at the next request, and so does revoking their access tokens.
Assistants also revoke their own tokens when the user disconnects, through the revocation endpoint.

Expired and revoked codes and tokens, and clients that registered but never obtained a token, stay in the database until cleared.
Clear them periodically with the ``mcpz oauth clear`` management command, or with the :func:`~tasks.clear_expired` task, as covered in :ref:`cleanup`.

.. _oauth-settings:

Settings
--------

All optional.

.. setting:: MCPZ_OAUTH_ACCESS_TOKEN_LIFETIME

A |timedelta|__, default one hour.

.. setting:: MCPZ_OAUTH_REFRESH_TOKEN_LIFETIME

A ``timedelta``, default thirty days.

.. setting:: MCPZ_OAUTH_DYNAMIC_REGISTRATION

Default ``True``.
Whether clients may register themselves through the registration endpoint.
The MCP specification prefers client ID metadata documents, which need no registration, but the hosted assistants still register dynamically, so leave this on unless you know your clients do not need it.

.. |timedelta| replace:: ``timedelta``
__ https://docs.python.org/3/library/datetime.html#datetime.timedelta

Private sites
-------------

Adding the app exposes a few new URLs on your site: the four endpoints under the path you chose, and the two discovery documents under ``/.well-known/``.
If your site is meant to be hard to find, here is what they reveal to someone who does not already know your MCP server’s URL.

The discovery documents are served only for real paths.
Asking for ``/.well-known/oauth-protected-resource`` with any path that is not an MCP server’s, or ``/.well-known/oauth-authorization-server`` with any path that is not the one you included the endpoints under, gets a plain 404, the same as any unknown URL on a Django site.
So a client has to know your MCP server’s URL first, and the discovery documents then tell it where the endpoints are, nothing more.
Since both paths are yours to choose, the endpoints can sit under the same unguessable prefix as everything else.

The one thing to check is |LOGIN_URL|__.
The authorize page sends anonymous users there, so if your login page is at a URL you would rather not reveal, such as an admin mounted at a secret path, point ``LOGIN_URL`` at a login page you are happy to show.

__ https://docs.djangoproject.com/en/stable/ref/settings/#login-url

Tested clients
--------------

The test suite runs the whole flow with the official MCP Python SDK client against a live server, through dynamic registration and through a client ID metadata document, including refreshing tokens.
The flow has also been run by hand with |mcp-remote|__, the bridge that many desktop clients use to reach remote servers, and with Claude Code calling tools through it.
Before relying on a hosted assistant, connect it to your own server once and go through the login and consent pages, since each assistant’s OAuth client has its own quirks.

.. |mcp-remote| replace:: ``mcp-remote``
__ https://www.npmjs.com/package/mcp-remote

Limitations
-----------

The app implements the parts of OAuth that MCP clients use, and nothing else.
In particular:

* Clients are public: there are no client secrets.
  MCP clients run on users’ machines or in the assistants’ browsers, where a secret could not be kept, so the protection comes from PKCE and exact redirect URIs instead.
* The only ways to obtain a token are the authorization code grant, meaning the login and consent flow, and refreshing.
  There is no client credentials grant for machine-to-machine access, no device grant, and no OpenID Connect.
* Scopes are accepted, shown on the consent page, and stored on tokens, but not interpreted.
  A token grants the same access as its user has, restricted per tool with :ref:`server-permissions` if needed.
* The endpoints send no CORS headers, since assistants and developer tools call them from servers or native code, not from web pages.
  A browser-based MCP client would need them: install |django-cors-headers|__ and allow the endpoints and the ``/.well-known/`` paths.

.. |django-cors-headers| replace:: ``django-cors-headers``
__ https://github.com/adamchainz/django-cors-headers
