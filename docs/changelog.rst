=========
Changelog
=========

1.0.2 (2026-09-16)
------------------

* Exempt the MCP server view and the OAuth views from Django’s |LoginRequiredMiddleware|__.
  Previously, projects using that middleware had it redirect unauthenticated requests to the login page, before the server or its OAuth views could apply their own authentication.

  .. |LoginRequiredMiddleware| replace:: ``LoginRequiredMiddleware``
  __ https://docs.djangoproject.com/en/stable/ref/middleware/#django.contrib.auth.middleware.LoginRequiredMiddleware

  `PR #20 <https://github.com/adamchainz/django-mcpz/pull/20>`__.

1.0.1 (2026-09-15)
------------------

* Allow ``Origin`` headers and OAuth resource URLs with IPv6 hosts, such as ``http://[::1]:8000``, to match their ``ALLOWED_HOSTS`` entries.
  Previously they were always rejected, since ``urlsplit()`` strips the brackets that ``ALLOWED_HOSTS`` entries keep.

  `PR #13 <https://github.com/adamchainz/django-mcpz/pull/13>`__.

* Send tool return values that serialize to JSON values other than objects, such as lists and numbers, as text content only, without ``structuredContent``.
  The specification requires ``structuredContent`` to be an object, and clients reject results where it is not.

  `PR #14 <https://github.com/adamchainz/django-mcpz/pull/14>`__.

* Reject malformed URLs with unbalanced brackets, such as ``http://[::1``, in OAuth redirect URIs, resource URLs, and client ID metadata document URLs, rather than failing with a 500 response.

  `PR #15 <https://github.com/adamchainz/django-mcpz/pull/15>`__.

* Reject OAuth ``resource`` and ``redirect_uri`` parameters longer than the fields they are stored in, rather than failing with a database error on databases that enforce field sizes.

  `PR #16 <https://github.com/adamchainz/django-mcpz/pull/16>`__.

* Support OAuth on sites deployed under a path prefix, with ``SCRIPT_NAME`` or ``FORCE_SCRIPT_NAME``.
  Resource URLs and the discovery document paths include the prefix, which is now stripped before matching them against the URLconf.

  `PR #17 <https://github.com/adamchainz/django-mcpz/pull/17>`__.

* Guess an ``Icon``’s MIME type from the path of its URL alone, ignoring any query string a static files storage appends.

  `PR #18 <https://github.com/adamchainz/django-mcpz/pull/18>`__.

1.0.0 (2026-09-15)
------------------

* Initial release.
