SQL queries
===========

.. currentmodule:: django_mcpz.sql

Language models write good SQL.
Rather than writing a tool for every question someone might ask of your data, you can let the model query it directly, with :func:`add_sql_tools`, which adds two tools to a server:

* ``sql_schema`` describes the tables the model may query, as ``CREATE TABLE`` statements with comments.
* ``sql_query`` runs one read-only ``SELECT`` statement and returns the rows.

This works with PostgreSQL only, through `psycopg 3 <https://www.psycopg.org/psycopg3/>`__.

Never give the model your application’s database role.
The model writes the SQL, and the SQL may come from whoever it is talking to, and whatever text it has read, so treat it as untrusted.
The tools are designed for a dedicated PostgreSQL role that can read a chosen set of views and nothing else.

Setup
-----

The setup takes five steps: views, models for them, a role, a database alias, and the tools.

1. Create PostgreSQL views exposing exactly the rows and columns the model should see, such as in a migration with |RunSQL|__.
   Views run with the privileges of their owner, so the role that queries them needs no access to the tables behind them.
   They’re also a stable interface, which the model can learn, decoupled from your tables.

   .. |RunSQL| replace:: ``RunSQL``
   __ https://docs.djangoproject.com/en/stable/ref/migration-operations/#runsql

   For example, the example project exposes orders without the customer’s free-text requests:

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

2. Define an unmanaged model for each view, so Django never tries to create or alter it.
   The models drive the ``sql_schema`` tool, so document them for the model reading it:

   * The model’s docstring, or its |db_table_comment|__, describes the table.
   * Each field’s |db_comment|__, or its ``help_text``, describes the column, as does its verbose name when it’s not just the field name with spaces.
   * Fields with ``choices`` list their values and labels.
   * Foreign keys to other models in the list become ``REFERENCES``, showing the model how to join.

   .. |db_table_comment| replace:: ``db_table_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/options/#db-table-comment

   .. |db_comment| replace:: ``db_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/fields/#db-comment

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

3. Create a role that can log in and read the views, and nothing more:

   .. code-block:: sql

       CREATE ROLE mcp_reader LOGIN PASSWORD '...';
       GRANT CONNECT ON DATABASE app TO mcp_reader;
       GRANT USAGE ON SCHEMA public TO mcp_reader;
       GRANT SELECT ON pizzeria_report_menu_item, pizzeria_report_order_line TO mcp_reader;

   Don’t make it a member of any other role, which it could switch to with ``SET ROLE``, and don’t grant it anything on ``PUBLIC`` beyond PostgreSQL’s defaults.
   Each query checks that the login role isn’t a superuser, can’t bypass row-level security, and isn’t a member of other roles, failing with ``ImproperlyConfigured`` otherwise.
   Remember to grant ``SELECT`` on new views as you add them, such as in the migrations that create them.

4. Add a database alias that connects as the role:

   .. code-block:: python

       DATABASES = {
           "default": {
               "ENGINE": "django.db.backends.postgresql",
               "NAME": "app",
               # ...
           },
           "mcp_reader": {
               "ENGINE": "django.db.backends.postgresql",
               "NAME": "app",
               "USER": "mcp_reader",
               "PASSWORD": os.environ["MCP_READER_PASSWORD"],
               # In tests, connect to the default alias’s test database, as
               # the reader role, rather than creating another.
               "TEST": {"MIRROR": "default"},
           },
       }

   The alias must log in as the role.
   Django’s ``assume_role`` option won’t do, since queries could switch back to the login role, so :func:`add_sql_tools` rejects it.

   In tests, the mirrored alias is still a separate connection, which sees only committed data, so test the tools with ``TransactionTestCase`` rather than ``TestCase``.
   The role also needs its grants on the test database, which granting in migrations takes care of.

   If you use a `database router <https://docs.djangoproject.com/en/stable/topics/db/multi-db/#database-routers>`__, make sure it doesn’t send migrations to this alias.

5. Add the tools to your server:

   .. code-block:: python

       from django_mcpz.sql import add_sql_tools
       from example.models import MenuItem, OrderLine

       add_sql_tools(
           server,
           models=[MenuItem, OrderLine],
           using="mcp_reader",
           context="MCPizza's sales data. Prices and totals are in pounds (GBP).",
       )

   ``context`` is optional text at the top of the schema, for anything that applies across tables.
   See :func:`add_sql_tools` for the other options.

What the model sees
-------------------

``sql_schema`` returns SQL, the format models know best for describing tables.
For the example project:

.. code-block:: sql

    -- MCPizza's sales data. Prices and totals are in pounds (GBP).

    -- Queries run with the time zone UTC, which applies to date functions on timestamp with time zone columns.

    -- Every pizza MCPizza has sold, including specials no longer on the menu.
    CREATE TABLE pizzeria_report_menu_item (
      id bigint NOT NULL PRIMARY KEY,
      name varchar(100) NOT NULL,
      price numeric(5, 2) NOT NULL,  -- Current price, in pounds.
      ...
    );

    -- One row per order. The customer’s free-text requests are not exposed, only whether there were any.
    CREATE TABLE pizzeria_report_order_line (
      id bigint NOT NULL PRIMARY KEY,
      pizza_id bigint NOT NULL REFERENCES pizzeria_report_menu_item (id),
      ...
      channel varchar(10) NOT NULL  -- Values: 'web' = Website, 'phone' = Telephone, 'mcp' = An AI assistant, over MCP.
    );

The views are shown as tables, since the difference doesn’t matter for reading them.

The time zone line comes from Django: with |USE_TZ|__ enabled, connections use UTC.
If your users think in another time zone, say so in ``context``, so the model knows to convert with ``AT TIME ZONE`` for questions like “orders per day”.

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

When rows are left out, the result adds ``"truncated": true`` and a ``"note"`` telling the model to aggregate or filter.
Rows are left out past ``max_rows``, or when the result would exceed ``max_size``.
Long values are shortened too, to a fair share of ``max_size``, so a single huge value can’t flood the model’s context.

Errors in the query, such as syntax errors, unknown columns, and permission denied, are returned to the model as tool errors, with PostgreSQL’s message, so it can correct itself.

Security
--------

The PostgreSQL role is the security boundary: the model can read whatever the role can.
The tools add more limits on top:

* Each query runs in a ``READ ONLY`` transaction, which is always rolled back.
  That reverts any settings the query changed with ``set_config()``.

* The query runs through a server-side cursor, as ``DECLARE ... CURSOR FOR <query>``, sent with PostgreSQL’s extended query protocol.
  This accepts one statement only, so no ``; COMMIT; ...`` tricks, and that statement must be a query: ``SELECT``, ``WITH``, ``VALUES``, or ``TABLE``.
  Data-modifying statements are rejected even inside ``WITH``.

* A ``statement_timeout`` stops long-running queries.
  Changing it with ``set_config()`` inside a query doesn’t affect that query, and the change is rolled back afterwards.

* Rows are fetched from the cursor in batches, stopping at ``max_rows``, once values totalling ``max_size`` have arrived, or once the timeout has passed.
  The first batch is a single row, and later ones are sized to fit what remains of ``max_size``, so a query for millions of rows, or for huge values, transfers only the first few.

* Session-level advisory locks, taken with functions like ``pg_advisory_lock()``, are released after each query, as they survive the rollback.

Some things the role can still do, given PostgreSQL’s defaults:

* Read the system catalogs, such as ``pg_views``, which shows the definitions of views, including the names of the tables and columns behind them, and ``pg_roles``, which lists role names.
  It can’t read the tables themselves, or other roles’ queries in ``pg_stat_activity``.

* See other sessions of the same role in ``pg_stat_activity``, including their SQL, and cancel or terminate them.
  Since every caller of the tools shares the role, one could see another’s queries, which may quote their conversation.
  To hide them, a superuser can run ``ALTER ROLE mcp_reader SET track_activities = off``.

* Take advisory locks, which are database-wide, for up to the timeout, which could block your application if it uses them.

* Use CPU, memory, and I/O for roughly up to twice the timeout, per call, as the query and fetching the rows are timed separately.
  A row is transferred in full, so a query returning a huge value, such as from ``repeat('x', 100000000)``, uses that much memory in your Django process before it is shortened.

Use your server’s ``auth`` and the ``permission`` argument to limit who can call the tools, and consider pointing the alias at a read replica.

The tools are marked as read-only, with ``readOnlyHint``, so clients may run them without asking the user each time.
