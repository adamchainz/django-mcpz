Deployment notes
================

.. _server-security:

Security
--------

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

* Each |tools/call|__ runs in the request like a normal view, so |ATOMIC_REQUESTS|__ applies to it, and a tool that raises rolls back if that setting is on.
  Without it, a tool that makes several writes should wrap them in |transaction.atomic()|__ itself.

* Requests carrying an ``Origin`` header from a host outside |ALLOWED_HOSTS|__ are rejected, so a web page cannot make a browser call your server with its cookies, as covered in :ref:`server-protocol-support`.

* OAuth clients identify themselves, through registration or a metadata document, so any application can present itself under any name.
  The consent page is where the user checks that the name and the redirect destination match the application they are connecting, which is why the :doc:`oauth <oauth>` app shows it on every authorization.

.. |SECURE_SSL_REDIRECT| replace:: ``SECURE_SSL_REDIRECT``
__ https://docs.djangoproject.com/en/stable/ref/settings/#secure-ssl-redirect

.. |tools/call| replace:: ``tools/call``
__ https://modelcontextprotocol.io/specification/draft/server/tools#calling-tools

.. |ATOMIC_REQUESTS| replace:: ``ATOMIC_REQUESTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#atomic-requests

.. |transaction.atomic()| replace:: ``transaction.atomic()``
__ https://docs.djangoproject.com/en/stable/topics/db/transactions/#django.db.transaction.atomic

.. |ALLOWED_HOSTS| replace:: ``ALLOWED_HOSTS``
__ https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts

.. _cleanup:

Cleanup
-------

Both the bearer tokens app and the OAuth app provide their cleanup as a sub-command of the ``mcpz`` management command, for running from cron, and as a task for Django’s |tasks framework|__, on Django 6.0 and later:

.. |tasks framework| replace:: tasks framework
__ https://docs.djangoproject.com/en/stable/topics/tasks/

.. code-block:: sh

    python manage.py mcpz bearer-tokens clear
    python manage.py mcpz oauth clear

.. code-block:: python

    from django_mcpz.oauth.tasks import clear_expired as clear_expired_oauth
    from django_mcpz.bearer_tokens.tasks import clear_expired as clear_expired_bearer_tokens

    clear_expired_bearer_tokens.enqueue()
    clear_expired_oauth.enqueue()

The tasks framework runs tasks but does not schedule them.
To run the cleanup on a schedule, use a scheduler for the framework, such as |django-scheduled-tasks|__, which wraps a task with a cron expression and runs it from its ``run_task_scheduler`` management command:

.. |django-scheduled-tasks| replace:: ``django-scheduled-tasks``
__ https://github.com/lode-braced/django-scheduled-tasks

.. code-block:: python

    from django_scheduled_tasks import cron_task

    from django_mcpz.oauth.tasks import clear_expired as clear_expired_oauth
    from django_mcpz.bearer_tokens.tasks import clear_expired as clear_expired_bearer_tokens

    cron_task(cron_schedule="0 4 * * *")(clear_expired_bearer_tokens)
    cron_task(cron_schedule="0 4 * * *")(clear_expired_oauth)

Put that in a module your project imports at startup, such as an app’s ``tasks.py``, so the schedules are registered.
Once a day is plenty: nothing depends on expired rows being gone, and the tables grow by one row per login or refresh.
