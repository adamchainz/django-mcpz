Installation
============

Requirements
------------

Python 3.10 to 3.15 supported.

Django 5.2 to 6.1 supported.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-mcpz

2. Add django-mcpz to your |INSTALLED_APPS|__:

   .. |INSTALLED_APPS| replace:: ``INSTALLED_APPS``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#installed-apps

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_mcpz",
           "django_mcpz.tokens",
           ...,
       ]

   ``django_mcpz.tokens`` is optional.
   It provides per-client tokens for authenticating MCP clients, the recommended way, and needs ``migrate`` running.
   See :ref:`server-tokens`.

Now you’re ready to make an MCP server object and route it, as covered in :doc:`servers`.
