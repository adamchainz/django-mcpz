.. _server-bearer-tokens:

Bearer tokens
=============

.. currentmodule:: django_mcpz.bearer_tokens

The optional ``django_mcpz.bearer_tokens`` app authenticates MCP clients that can send a pasted credential, which is how developer tools work: one bearer token per client, each acting as a user, revocable one at a time.
It stores only a hash of each token, so a leaked database dump does not reveal usable credentials, like Django does for passwords.

To use it, add the app to ``INSTALLED_APPS`` and run ``migrate``:

.. code-block:: python

    INSTALLED_APPS = [
        ...,
        "django_mcpz",
        "django_mcpz.bearer_tokens",
        ...,
    ]

Then pass its authentication callable to your server:

.. code-block:: python

    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth

    server = MCPServer(
        name="shop",
        version="1.0.0",
        auth=token_auth,
    )

Create bearer tokens with the ``mcpz bearer-tokens create`` management command, which takes a name for the token and a user to associate it with and prints the token value once:

.. code-block:: sh

    python manage.py mcpz bearer-tokens create "Claude Code" --user alice

Alternatively, you can create tokens in the admin, which shows the token value once in a message at the top of the page.

Bearer tokens last until revoked by default.
Pass ``--expires-in-days`` to the command, or set :attr:`~models.Token.expires_at` in the admin, for tokens that stop working on their own, at the cost of reissuing them:

.. code-block:: sh

    python manage.py mcpz bearer-tokens create "Claude Code" --user alice --expires-in-days 90

Configure the MCP client to send the value in the ``Authorization`` header, as ``Authorization: Bearer mcp_...``.
Revoke a token in the admin, or with :meth:`Token.revoke() <models.Token.revoke>`, and its client is cut off from the next request.

Expired and revoked bearer tokens stay in the database until cleared, so that the admin shows what happened to them.
Clear them periodically with the ``mcpz bearer-tokens clear`` management command, or with the :func:`~tasks.clear_expired` task, as covered in :ref:`cleanup`.

.. _server-bearer-tokens-clients:

Which clients can use bearer tokens
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Bearer tokens suit clients whose configuration accepts custom headers, which is the case for developer tools: Claude Code, Codex, Cursor, and the |llm|__ tool, among others.
For example, in Claude Code:

.. |llm| replace:: ``llm``
__ https://llm.datasette.io/

.. code-block:: sh

    claude mcp add \
        --transport http \
        --header "Authorization: Bearer mcp_..." \
        shop \
        https://example.com/mcp

Hosted assistants and their desktop apps, such as Claude.ai and ChatGPT, offer no way to enter a header when adding a server, so bearer tokens do not work with them.
They connect through OAuth, which the :doc:`oauth` app provides.
A server can serve both kinds of client with an ``auth`` callable that tries each app’s callable in turn.

The app’s model, authentication callable, and command are documented in :ref:`api-bearer-tokens`.
