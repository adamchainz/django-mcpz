Example Application: MCPizza
============================

An example project serving a local MCP server for a pizza place called **MCPizza** (not to be confused with `McPizza <https://mcdonalds.fandom.com/wiki/McPizza>`__).

The example is intended to show how to build an MCP server and a use case that allows an LLM to make decisions based on freeform text in a database.
Each ``Pizza`` has structured fields the server can filter and enforce, such as price and the dates a special runs between, as well as freeform ``notes`` that an LLM can act on, such as “Vegan cheese available on request”.

The important code lives in these files:

* ``pizzeria/models.py``: the models.
* ``pizzeria/migrations/0002_menu.py``: the migration that seeds data.
  Specials are dated relative to the day you run the migrations.
* ``pizzeria/mcp.py``: the MCP server and tools.

The three tools are:

* ``current_date``: today’s date and weekday, so an LLM can work out what relative dates like “tomorrow” mean.
* ``search_menu``: the pizzas available on a date, with an optional name query and price limit, returning the notes for the model to read.
* ``place_order``: order a pizza from today’s menu, with a ``requests`` field for whatever the model gleaned from the notes.
  The server enforces the menu, rejecting pizzas not available today with an in-band error the model can act on.

Setup
-----

In this directory, run:

.. code-block:: sh

    uv run manage.py migrate
    uv run manage.py runserver

Migrating creates an SQLite database seeded with the menu.
The ``runserver`` should launch the MCP server at http://127.0.0.1:8066/mcp.

This example is unauthenticated (``auth=public``) and served on localhost only, as it’s a local demo, not something to expose to a network.
See the `authentication docs <https://django-mcpz.readthedocs.io/en/latest/servers.html#authentication>`__ for adding auth.

Once the server is running, any MCP client should be able to connect.

Test with ``llm``
-----------------

`llm <https://pypi.org/project/llm/>`__ is a command-line interface for LLMs that supports MCP.
Set it up with an appropriate plugin for your model of choice and use the `llm-tools-mcp <https://pypi.org/project/llm-tools-mcp/>`__ plugin to connect to the MCPizza server.

For example, using Claude, running llm through `uvx <https://docs.astral.sh/uv/guides/tools/>`__:

.. code-block:: console

    $ LLM_TOOLS_MCP_CONFIG_DIR=. \
      uvx --with llm-anthropic --with llm-tools-mcp --with "mcp<2" \
      llm -m claude-sonnet-5 -T MCP \
      "Order me a mix of pizzas to feed up to 70 attendees at Django London."

Note:

* ``LLM_TOOLS_MCP_CONFIG_DIR=.`` points llm-tools-mcp at this directory, so it picks up the ``mcp.json`` file describing the server.

* `llm-anthropic <https://pypi.org/project/llm-anthropic/>`__ is an ``llm`` plugin to connect to Claude.

* `llm-tools-mcp <https://pypi.org/project/llm-tools-mcp/>`__ is an ``llm`` plugin to connect to MCP servers.

* ``mcp<2`` is a temporary workaround to paper over llm-tools-mcp’s lack of support for the new version at time of writing.

* ``-m claude-sonnet-5`` selects the model to use.

* ``-T MCP`` tells llm to use the MCP plugin, which is required for tool calls.

Adapt the command for your model of choice.

Here’s an example run (with thinking traces omitted):

.. code-block:: text

    I should check today's date first, then look at the menu options to plan a pizza order for 70 people.

    ...

    Done! Ordered **24 pizzas** for Django London (≈70 attendees, ~2.7 slices/person):

    | Pizza | Qty | Notes | Total |
    |---|---|---|---|
    | Margherita of Theseus | 5 | 2 with vegan cheese | £47.50 |
    | Four Cheese, Five If You're Nice | 3 | — | £36.00 |
    | The Off-By-One | 3 | 1 with vegan cheese | £31.50 |
    | Pepperoni Overflow | 4 | — | £50.00 |
    | The Hawaiian Standoff | 2 | — | £22.00 |
    | Nduja Believe It | 2 | Spicy | £26.00 |
    | Null Pointer | 2 | 1 gluten-free | £12.00 |
    | Garlic Bread (Technically a Pizza) | 3 | 1 gluten-free | £16.50 |

    **Grand total: £241.50**

    Mix covers: vegetarian majority, vegan-cheese options, a gluten-free base or two, classic pepperoni/Hawaiian for meat-eaters, and a spicy nduja option — should keep a diverse Django meetup crowd happy. Let me know if you'd like to adjust quantities or add anything (e.g., more spicy/vegan coverage).

Add ``--td`` to the command to enable tool debugging, which prints the model’s tool calls and the server’s responses among the output, like:

.. code-block:: text

    Tool call: place_order({'pizza': 'Margherita of Theseus', 'quantity': 5, 'requests': '2 with vegan cheese please, rest standard'})
      [TextContent(type='text', text='{"order_id":3,"pizza":"Margherita of Theseus","quantity":5,"requests":"2 with vegan cheese please, rest standard","total":47.5}', annotations=None, meta=None)]

Test with ``claude``
--------------------

If you have `Claude Code <https://claude.com/claude-code>`__ set up, you can test with its CLI too.
Run:

.. code-block:: sh

    claude --mcp-config mcp.json --strict-mcp-config --allowedTools "mcp__mcpizza__*" \
      -p "What vegetarian pizzas could I order tomorrow for under \$12? I'd prefer vegan if they can do it."

Note:

* ``--mcp-config mcp.json`` points the model at the ``mcp.json`` file in this directory that describes the server.

* ``--strict-mcp-config`` tells the model to reject any server that doesn’t match the config exactly.

* ``--allowedTools "mcp__mcpizza__*"`` restricts the model to only the tools in this server, so it can’t call other tools on other servers.

Here’s an example answer from a model, after it called the ``current_date`` and ``search_menu`` MCP tools:

.. code-block:: text

    For tomorrow (2026-09-04), the vegetarian options under $12 are:

    - **Null Pointer** – $6 — plain base, vegan by default (no cheese/toppings)
    - **Garlic Bread (Technically a Pizza)** – $5.50 — vegetarian; GF base available, but not noted as vegan-adaptable
    - **Margherita of Theseus** – $9.50 — vegetarian, **vegan cheese available on request**
    - **The Off-By-One** (mushrooms, olives, red onion) – $10.50 — vegetarian, **vegan cheese available on request**

    Since you'd prefer vegan: **Margherita of Theseus** or **The Off-By-One** both work with vegan cheese swapped in, and **Null Pointer** is vegan as-is (though it's just a plain base). Want me to place an order for one of these?

Write tools work too.
This creates a real ``Order`` row, with the model’s reading of the notes in its ``requests`` field:

.. code-block:: sh

    claude --mcp-config mcp.json --strict-mcp-config --allowedTools "mcp__mcpizza__*" \
      -p "I'm gluten-free. Order me two of whatever you'd recommend from today's menu and make sure the kitchen knows about the base. Tell me the order ID and total."

Example output:

.. code-block:: text

    Order placed: **2× Garlic Bread (Technically a Pizza)** on gluten-free bases (kitchen note included explicitly).

    - **Order ID:** 1
    - **Total:** $11.00

Talking to it from Python
-------------------------

The official `MCP Python SDK <https://pypi.org/project/mcp/>`__ (version 2+) can connect like so:

.. code-block:: python

    import asyncio

    from mcp.client.client import Client


    async def main():
        async with Client("http://127.0.0.1:8066/mcp") as client:
            tools = await client.list_tools()
            print([tool.name for tool in tools.tools])
            result = await client.call_tool("search_menu", {"max_price": 10})
            print(result.structured_content)


    asyncio.run(main())
