.. _server-tokens:

Tokens
======

.. currentmodule:: django_mcpz.tokens

The recommended way to authenticate MCP clients is with tokens from the optional ``django_mcpz.tokens`` app: one token per client, each acting as a user, revocable one at a time.
It stores only a hash of each token, so a leaked database dump does not reveal usable credentials, like Django does for passwords.

To use it, add the app to ``INSTALLED_APPS`` and run ``migrate``:

.. code-block:: python

    INSTALLED_APPS = [
        ...,
        "django_mcpz",
        "django_mcpz.tokens",
        ...,
    ]

Then pass its authentication callable to your server:

.. code-block:: python

    from django_mcpz.server import MCPServer
    from django_mcpz.tokens.auth import token_auth

    server = MCPServer(name="shop", version="1.0.0", auth=token_auth)

Create tokens with the ``create_mcp_token`` management command, which prints the token value once, or in the admin, which shows it once in a message:

.. code-block:: sh

    python manage.py create_mcp_token "Claude Code" --user alice

Tokens last until revoked by default.
Pass ``--expires-in-days`` to the command, or set :attr:`~models.Token.expires_at` in the admin, for tokens that stop working on their own, at the cost of reissuing them:

.. code-block:: sh

    python manage.py create_mcp_token "Claude Code" --user alice --expires-in-days 90

Configure the MCP client to send the value as a bearer token, in the ``Authorization`` header.
Revoke a token in the admin, or with :meth:`Token.revoke() <models.Token.revoke>`, and its client is cut off from the next request.

.. _server-tokens-clients:

Which clients can use tokens
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Tokens suit clients whose configuration accepts custom headers, which is the case for developer tools: Claude Code, Codex, Cursor, and the |llm|__ tool, among others.
For example, in Claude Code:

.. |llm| replace:: ``llm``
__ https://llm.datasette.io/

.. code-block:: sh

    claude mcp add --transport http shop https://example.com/mcp --header "Authorization: Bearer mcp_..."

Hosted assistants and their desktop apps, such as Claude.ai and ChatGPT, offer no way to enter a header when adding a server, so tokens do not work with them.
They connect to servers that are public, or that implement the OAuth authorization described in the `MCP authorization specification <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization>`__, which django-mcpz does not currently implement.
If your users are on those clients, tokens alone will not get them connected.

The app’s model, authentication callable, and command are documented in :ref:`api-tokens`.
