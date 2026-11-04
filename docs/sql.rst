SQL tools
=========

.. currentmodule:: django_mcpz.sql.tools

LLMs write good SQL.
Rather than writing a tool for every question someone might ask of your data, you can let the LLM query it directly, with :func:`add_sql_tools`, which adds two tools to a server:

* ``sql_schema`` lists the tables the LLM may query, then describes the columns of the ones it picks, as ``CREATE TABLE`` statements with comments.
* ``sql_query`` runs one read-only ``SELECT`` statement and returns the rows.

This works with PostgreSQL, through `psycopg 3 <https://www.psycopg.org/psycopg3/>`__, and SQLite.

The LLM writes the SQL, and the SQL may come from whoever it is talking to, and whatever text it has read, so treat it as untrusted.
Before running each query, the tools parse it and check it reads only the tables and columns you list, and calls only safe functions, as covered in :ref:`sql-checking`.

Setup
-----

1. Define a :class:`~django_mcpz.sql.readers.SQLReader` alongside your MCP server, in your app’s ``mcp`` module, listing the columns queries may read, as a dict of models to field names:

   .. code-block:: python

       from django_mcpz.sql.readers import SQLReader
       from example.models import Order, Pizza

       reports_reader = SQLReader(
           tables={
               Pizza: ["id", "name", "price", "vegetarian"],
               Order: ["id", "pizza_id", "quantity", "total", "placed_at"],
           },
       )

   Each field is a column of the model’s table, named by the field’s name, or, for foreign keys, its attribute name, like ``pizza_id``.
   Only the named columns are exposed, so adding a field to a model later never exposes its column, such as ``Order.requests`` here, which holds customers’ free text.

   Queries run through the ``default`` database alias, unless you pick another with ``using``.

   Listing some columns of tables works for many cases.
   To expose some rows, derived values, or joins, create a view, such as in a migration with |RunSQL|__, define an unmanaged model for it, and list that.
   Views also make a stable interface, which the LLM can learn, decoupled from your tables.
   For example, the example project exposes whether each order has requests, but not their text:

   .. code-block:: python

       migrations.RunSQL(
           """
           CREATE VIEW pizzeria_report_order_line AS
           SELECT
               id,
               pizza_id,
               quantity,
               total,
               placed_at,
               requests <> '' AS has_requests,
               channel
           FROM pizzeria_order
           """,
           "DROP VIEW pizzeria_report_order_line",
       )

   .. code-block:: python

       class OrderLine(models.Model):
           """
           One row per order. The customer’s free-text requests are not exposed,
           only whether there were any.
           """

           id = models.BigIntegerField(primary_key=True)
           pizza = models.ForeignKey(MenuItem, on_delete=models.DO_NOTHING)
           quantity = models.PositiveSmallIntegerField()
           total = models.DecimalField(
               max_digits=7,
               decimal_places=2,
               help_text="Price paid for the whole order, in pounds.",
           )
           ...

           class Meta:
               managed = False
               db_table = "pizzeria_report_order_line"

   .. |RunSQL| replace:: ``RunSQL``
   __ https://docs.djangoproject.com/en/stable/ref/migration-operations/#runsql

   The models and fields also drive the ``sql_schema`` tool, so document them for the LLM reading it:

   * The model’s docstring, or its |db_table_comment|__, describes the table.
   * Each field’s |db_comment|__, or its ``help_text``, describes the column, as does its verbose name when it’s not just the field name with spaces.
   * Fields with ``choices`` list their values and labels.
   * Foreign keys to listed columns of other listed tables become ``REFERENCES``, showing the LLM how to join.

   .. |db_table_comment| replace:: ``db_table_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/options/#db-table-comment

   .. |db_comment| replace:: ``db_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/fields/#db-comment

2. Add the tools to your server, with the reader:

   .. code-block:: python

       from django_mcpz.sql.tools import add_sql_tools

       add_sql_tools(
           server,
           reader=reports_reader,
           context="MCPizza's sales data. Prices and totals are in pounds (GBP).",
       )

   ``context`` is optional text at the top of the table list, for anything that applies across tables.
   See :func:`add_sql_tools` for the other options.

Who sees what
-------------

By default, every call sees all the reader’s tables.
Pass ``view_permissions=True`` to the reader to limit each call to the tables whose models its user has the view permission for, ``<app_label>.view_<model_name>``, as the admin checks:

.. code-block:: python

    reports_reader = SQLReader(
        tables={
            Pizza: ["id", "name", "price", "vegetarian"],
            Order: ["id", "pizza_id", "quantity", "total", "placed_at"],
        },
        view_permissions=True,
    )

Then grant users and groups the view permissions for the tables they may query.
``sql_schema`` lists and describes only their tables, and ``sql_query`` checks queries against only those.
Users without any see an empty table list.

.. _sql-checking:

Checking queries
----------------

``sql_query`` checks each query before it runs, with a Rust extension included in django-mcpz, which parses it with `sqlparser <https://github.com/apache/datafusion-sqlparser-rs>`__.
The checker is conservative, allowing only what it fully understands:

* One ``SELECT``, optionally with ``WITH``, and ``UNION``, ``INTERSECT``, or ``EXCEPT``.
  No other statements, no ``WITH RECURSIVE``, locking clauses, ``SELECT INTO``, or ``LATERAL``.

* Tables, by unqualified name, of those the call can see, plus the query’s own ``WITH`` tables and subqueries, which need aliases.

* Columns, named, rather than ``*``, of the reader’s listed columns.
  An unqualified column must be listed for exactly one table of the innermost query with tables, since the database resolves names there first, possibly to a column the checker doesn’t know about.
  Otherwise, qualify it with its table’s name or alias.

* Joins, with ``ON`` or ``USING``, but not ``NATURAL``.

* Functions from a list per database: aggregates, window functions, and common functions on numbers, text, dates, and JSON, such as ``count()``, ``round()``, ``coalesce()``, ``lower()``, ``date_trunc()`` on PostgreSQL, and ``strftime()`` on SQLite.
  Mostly the same on both, so queries work on either, and none reach beyond the query’s own data, such as into settings, files, or other sessions.

* Arithmetic, comparison, ``LIKE``, ``->``, and ``->>`` operators, ``CASE``, ``CAST`` to plain types, plain numbers, and ``'single-quoted'`` strings.

Anything else is returned to the LLM as an error saying what isn’t allowed, so it can rewrite the query.

The query that runs is the checked one, rendered back to SQL from what the checker parsed, rather than the original text.
So the database runs what was checked, even where its own parser would read the text differently.

What the LLM sees
-----------------

``sql_schema`` works in two steps, so a large schema doesn’t fill the LLM’s context up front.
Called without arguments, it lists the tables, each with its description from its model.
For the example project:

.. code-block:: text

    MCPizza's sales data. Prices and totals are in pounds (GBP).

    Queries run with the time zone UTC, which applies to date functions on timestamp with time zone columns.

    Tables:
    - pizzeria_report_menu_item: Every pizza MCPizza has sold, including specials no longer on the menu.
    - pizzeria_report_order_line: One row per order. The customer’s free-text requests are not exposed, only whether there were any.

    Call sql_schema with tables to see their columns.

The LLM then calls it again with the tables it needs, such as ``{"tables": ["pizzeria_report_order_line"]}``, and gets their columns as SQL, the format LLMs know best for describing tables:

.. code-block:: sql

    -- One row per order. The customer’s free-text requests are not exposed, only whether there were any.
    CREATE TABLE pizzeria_report_order_line (
      id bigint NOT NULL PRIMARY KEY,
      pizza_id bigint NOT NULL REFERENCES pizzeria_report_menu_item (id),
      quantity smallint NOT NULL,
      total numeric(7, 2) NOT NULL,  -- Price paid for the whole order, in pounds.
      ...
      channel varchar(10) NOT NULL  -- Values: 'web' = Website, 'phone' = Telephone, 'mcp' = An AI assistant, over MCP.
    );

So the table descriptions are the LLM’s guide to which tables to look at: keep them short, but say what a row is.

Views are shown as tables too, since the difference doesn’t matter for reading them.

The time zone line in the table list comes from Django: with |USE_TZ|__ enabled, connections use UTC.
If your users think in another time zone, say so in ``context``, so the LLM knows to convert with ``AT TIME ZONE`` for questions like “orders per day”.

.. |USE_TZ| replace:: ``USE_TZ``
__ https://docs.djangoproject.com/en/stable/ref/settings/#std-setting-USE_TZ

``sql_query`` returns JSON, with the column names once and each row as an array:

.. code-block:: json

    {
      "columns": ["name", "revenue"],
      "rows": [["Pepperoni Overflow", 1425], ["Nduja Believe It", 1261]]
    }

This is nearly as compact as CSV, which saves only around 10–15% on typical results, but keeps types and nulls unambiguous.
Decimals are numbers, except NaN and infinities, which are strings, as JSON has no numbers for them.
Dates and times are ISO 8601 strings, ``json`` and ``jsonb`` columns are nested JSON, and other types, like ranges, are in their PostgreSQL text form.

When rows are left out, the result adds ``"truncated": true`` and a ``"note"`` telling the LLM to aggregate or filter.
Rows are left out past ``max_rows``, or when the result would exceed ``max_size``.
Long values are shortened too, to a fair share of ``max_size``, so a single huge value can’t flood the LLM’s context.

Errors in the query, such as syntax errors and checks it fails, are returned to the LLM as tool errors, so it can correct itself.
Database errors are about the query as checked, which can differ from the LLM’s, such as without comments, so when it does, the error includes it.

Security
--------

The checker is the security boundary: queries run through the reader’s database alias, as its user, so a query the checker wrongly allowed could read anything that user can.
For more defence, point ``using`` at an alias whose user can only read, or at a read replica.

On top of the checker, the tools limit what a query can do beyond reading:

* Each query runs on a fresh connection to the alias’s database, closed afterwards.
  So it sees only committed data, not uncommitted changes in the current request’s transaction, and leaves your app’s own connection untouched.
  Hooks you add to the alias’s connection with |execute_wrapper|__ apply to it too.

  On PostgreSQL, opening a connection takes several milliseconds, and tens over a network with TLS.
  To avoid that, use Django’s |connection pool|__, with the ``"pool"`` option, and each query borrows a pooled connection, returning it afterwards, rolled back.
  Size the pool for it: during a query, the request holds two connections, its own, if it has used the database, and the query’s.

  .. |connection pool| replace:: connection pool
  __ https://docs.djangoproject.com/en/stable/ref/databases/#postgresql-pool

* On PostgreSQL, the query runs in a ``READ ONLY`` transaction, which is never committed.

* On PostgreSQL, the query runs through a server-side cursor, as ``DECLARE ... CURSOR FOR <query>``, sent with PostgreSQL’s extended query protocol.
  This accepts one statement only, and that statement must be a query.

* On PostgreSQL, a watchdog thread cancels the query once the timeout has passed, including fetching its rows, and a ``statement_timeout`` stops each statement that runs too long.

* On SQLite, the connection is set to ``PRAGMA query_only``.
  A progress handler on it cancels queries that run longer than the timeout, and a length limit stops any query making a string or blob longer than 16 MiB.
  A private in-memory database, ``":memory:"``, can’t work, since each connection to one gets its own, empty, database.

* Rows are fetched from the cursor in batches, stopping at ``max_rows``, or once values totalling ``max_size`` have arrived.
  The first batch is a single row, and later ones are sized to fit what remains of ``max_size``, so a query for millions of rows, or for huge values, transfers only the first few.

.. |execute_wrapper| replace:: ``execute_wrapper()``
__ https://docs.djangoproject.com/en/stable/topics/db/instrumentation/

A query can still use CPU, memory, and I/O for up to the timeout, per call.

Use your server’s ``auth`` and the ``permission`` argument to limit who can call the tools.

The tools are marked as read-only, with ``readOnlyHint``, so clients may run them without asking the user each time.
