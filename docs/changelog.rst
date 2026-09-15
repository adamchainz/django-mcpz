=========
Changelog
=========

Unreleased
----------

* Allow ``Origin`` headers and OAuth resource URLs with IPv6 hosts, such as ``http://[::1]:8000``, to match their ``ALLOWED_HOSTS`` entries.
  Previously they were always rejected, since ``urlsplit()`` strips the brackets that ``ALLOWED_HOSTS`` entries keep.

1.0.0 (2026-09-15)
------------------

* Initial release.
