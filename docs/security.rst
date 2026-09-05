.. _server-security:

Security notes
==============

An MCP server lets a language model run code in your project on behalf of whoever holds the credentials.
Some things to keep in mind:

* Serve over HTTPS only.
  Bearer tokens travel in a header on every request, so a plain HTTP server exposes them to anyone on the network path.
  Django’s |SECURE_SSL_REDIRECT|__ setting covers the basics.

* A token is a password for its user.
  Anyone holding it can call every tool that user can, from any machine, until it is revoked.
  Treat token values like passwords when creating and sharing them, and revoke tokens for lost devices and departed users, or deactivate the user.

* Tool functions run with the caller’s ``request.user`` and no further checks.
  Anything a tool reads or changes is available to every authenticated client, unless the tool checks permissions itself or is restricted with ``permission``, see :ref:`server-permissions`.
  Model tools on what the user should be able to do, not on what the model asks for.

* Tool annotations such as ``read_only`` and ``destructive`` are hints for the client’s user interface, not enforcement.
  A tool marked read-only is only read-only if its code is.

* Whatever a tool returns goes into the model’s context, where it competes with the user’s instructions.
  Data your application stores, such as free text from other users, can contain instructions that try to steer the model.
  Keep tools narrow, return only the fields the model needs, and prefer structured output over pasting whole documents.

* Each ``tools/call`` runs in the request like a normal view, so |ATOMIC_REQUESTS|__ applies to it, and a tool that raises rolls back if that setting is on.
  Without it, a tool that makes several writes should wrap them in |transaction.atomic()|__ itself.

* Requests carrying an ``Origin`` header from a host outside |ALLOWED_HOSTS|__ are rejected, so a web page cannot make a browser call your server with its cookies, as covered in :ref:`server-protocol-support`.

.. |SECURE_SSL_REDIRECT| replace:: ``SECURE_SSL_REDIRECT``
__ https://docs.djangoproject.com/en/stable/ref/settings/#secure-ssl-redirect

.. |ATOMIC_REQUESTS| replace:: ``ATOMIC_REQUESTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#atomic-requests

.. |transaction.atomic()| replace:: ``transaction.atomic()``
__ https://docs.djangoproject.com/en/stable/topics/db/transactions/#django.db.transaction.atomic

.. |ALLOWED_HOSTS| replace:: ``ALLOWED_HOSTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts
