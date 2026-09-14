===========
django-mcpz
===========

.. image:: https://img.shields.io/readthedocs/django-mcpz?style=for-the-badge
   :target: https://django-mcpz.readthedocs.io/en/latest/

.. image:: https://img.shields.io/github/actions/workflow/status/adamchainz/django-mcpz/main.yml.svg?branch=main&style=for-the-badge
   :target: https://github.com/adamchainz/django-mcpz/actions?workflow=CI

.. image:: https://img.shields.io/badge/Coverage-100%25-success?style=for-the-badge
   :target: https://github.com/adamchainz/django-mcpz/actions?workflow=CI

.. image:: https://img.shields.io/pypi/v/django-mcpz.svg?style=for-the-badge
   :target: https://pypi.org/project/django-mcpz/

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
   :target: https://github.com/psf/black

.. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=for-the-badge
   :target: https://github.com/pre-commit/pre-commit
   :alt: pre-commit

----

*Easy peasy MCP servers in Django.*

django-mcpz lets you build a Model Context Protocol (MCP) server in your Django project.
Create an MCP server object with metadata, give it a URL, and attach Python functions to it as tools.
The MCP server acts like a regular synchronous Django view, handling the intricacies of the MCP protocol.
django-mcpz leans on `msgspec <https://msgspec.dev/>`__ for defining schemas and fast JSON serialization and deserialization.

A quick example:

.. code-block:: python

    from typing import Literal

    import msgspec

    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth
    from example.models import Order

    server = MCPServer(
        name="shop",
        version="1.0.0",
        instructions="Query the shop’s order database.",
        auth=token_auth,
    )


    class CountOrdersParams(msgspec.Struct):
        status: Literal["pending", "shipped", "cancelled"] | None = None


    class CountOrdersResult(msgspec.Struct):
        count: int


    @server.tool(
        description="Count Order rows, optionally filtered by status.",
        read_only=True,
    )
    def count_orders(request, params: CountOrdersParams) -> CountOrdersResult:
        qs = Order.objects.all()
        if params.status is not None:
            qs = qs.filter(status=params.status)
        return CountOrdersResult(count=qs.count())

Documentation
-------------

Please see https://django-mcpz.readthedocs.io/.
