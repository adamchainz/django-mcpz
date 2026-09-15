=========
Changelog
=========

Unreleased
----------

* Allow ``Origin`` headers and OAuth resource URLs with IPv6 hosts, such as ``http://[::1]:8000``, to match their ``ALLOWED_HOSTS`` entries.
  Previously they were always rejected, since ``urlsplit()`` strips the brackets that ``ALLOWED_HOSTS`` entries keep.

* Send tool return values that serialize to JSON values other than objects, such as lists and numbers, as text content only, without ``structuredContent``.
  The specification requires ``structuredContent`` to be an object, and clients reject results where it is not.

1.0.0 (2026-09-15)
------------------

* Initial release.
