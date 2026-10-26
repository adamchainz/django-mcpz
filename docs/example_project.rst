Example project
===============

The django-mcpz repository contains an example project in the |example directory|__.

It serves an unauthenticated, local-only MCP server for a pizza place called **MCPizza**.
The menu data mixes structured fields the server enforces, such as prices and the dates specials run, with freeform notes suitable for an LLM to act on, such as “Gluten-free base available, ask when ordering”.

The example project README walks through running the project and querying it with ``llm`` and ``claude``.
Give it a try and inspect the code!

.. |example directory| replace:: ``example/`` directory
__ https://github.com/adamchainz/django-mcpz/tree/main/example
