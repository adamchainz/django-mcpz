SQL queries
===========

.. currentmodule:: django_mcpz.sql

Language models write good SQL.
Rather than writing a tool for every question someone might ask of your data, you can let the model query it directly, with :func:`add_sql_tools`, which adds two tools to a server:

* ``sql_schema`` lists the tables the model may query, then describes the columns of the ones it picks, as ``CREATE TABLE`` statements with comments.
* ``sql_query`` runs one read-only ``SELECT`` statement and returns the rows.

This works with PostgreSQL only, through `psycopg 3 <https://www.psycopg.org/psycopg3/>`__.

Never give the model your application’s database role.
The model writes the SQL, and the SQL may come from whoever it is talking to, and whatever text it has read, so treat it as untrusted.
The tools are designed for a dedicated PostgreSQL role that can read chosen tables, views, and columns, and nothing else.

Setup
-----

The setup takes four steps: models describing what to expose, a database alias for the role, a ``post_migrate`` handler that maintains the role, and the tools.

1. Choose what to expose, as a list of models.
   Each model’s table, and only the columns of its fields, become readable:

   * **A whole table:** use your existing model.
     Every column of its table is exposed.

   * **Some columns of a table:** define an unmanaged model with the same ``db_table`` and only the fields to expose.
     Columns without a field stay unreadable, such as ``requests`` here:

     .. code-block:: python

         class OrderSummary(models.Model):
             """
             One row per order, without the customer’s requests.
             """

             id = models.BigIntegerField(primary_key=True)
             pizza = models.ForeignKey(Pizza, on_delete=models.DO_NOTHING)
             quantity = models.PositiveSmallIntegerField()
             placed_at = models.DateTimeField()

             class Meta:
                 managed = False
                 db_table = "pizzeria_order"

   * **Some rows, or derived columns:** create a view, such as in a migration with |RunSQL|__, and define an unmanaged model for it.
     Views run with the privileges of their owner, so the role needs no access to the tables behind them.
     They also make a stable interface, which the model can learn, decoupled from your tables.
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

   The models also drive the ``sql_schema`` tool, so document them for the model reading it:

   * The model’s docstring, or its |db_table_comment|__, describes the table.
   * Each field’s |db_comment|__, or its ``help_text``, describes the column, as does its verbose name when it’s not just the field name with spaces.
   * Fields with ``choices`` list their values and labels.
   * Foreign keys to other models in the list become ``REFERENCES``, showing the model how to join.

   .. |db_table_comment| replace:: ``db_table_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/options/#db-table-comment

   .. |db_comment| replace:: ``db_comment``
   __ https://docs.djangoproject.com/en/stable/ref/models/fields/#db-comment

2. Add a database alias that connects as the role, which the next step creates:

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

   The alias must log in as its own role.
   Django’s ``assume_role`` option won’t do, since queries could switch back to the login role, so :func:`add_sql_tools` rejects it.

   If you use a `database router <https://docs.djangoproject.com/en/stable/topics/db/multi-db/#database-routers>`__, make sure it doesn’t send migrations to this alias.

3. Maintain the role’s grants with :func:`sync_sql_grants`, from a |post_migrate|__ handler, so they follow your models as they change:

   .. |post_migrate| replace:: ``post_migrate``
   __ https://docs.djangoproject.com/en/stable/ref/signals/#post-migrate

   .. code-block:: python

       from django.apps import AppConfig
       from django.db.models.signals import post_migrate


       class ExampleConfig(AppConfig):
           name = "example"

           def ready(self):
               post_migrate.connect(sync_mcp_reader, sender=self)


       def sync_mcp_reader(using, **kwargs):
           from django_mcpz.sql import sync_sql_grants
           from example.models import MenuItem, OrderLine

           if using == "default":
               sync_sql_grants(
                   models=[MenuItem, OrderLine],
                   reader="mcp_reader",
                   using=using,
               )

   After every ``migrate``, this:

   * creates the role if it doesn’t exist, with the alias’s ``PASSWORD``;
   * grants it ``CONNECT`` on the database and ``USAGE`` on the tables’ schemas;
   * revokes everything previously granted to it on tables and columns, then grants ``SELECT`` on exactly the columns of the models’ fields.

   It all happens in one transaction, so the role never sees a half-way state.
   The ``default`` connection’s role needs permission to create roles, or create the role yourself beforehand, and must own the tables, or be able to grant on them.
   It also runs when the test runner creates the test database, so the role has its grants there too.

   Use the same list of models here and for the tools.
   With column-level grants, ``SELECT *`` on a table with unexposed columns fails with “permission denied”, but the schema lists only the exposed columns, so models name them instead.

   Don’t make the role a member of any other role, which it could switch to with ``SET ROLE``.
   Each query checks that the login role isn’t a superuser, can’t bypass row-level security, and isn’t a member of other roles, failing with ``ImproperlyConfigured`` otherwise.

4. Add the tools to your server:

   .. code-block:: python

       from django_mcpz.sql import add_sql_tools
       from example.models import MenuItem, OrderLine

       add_sql_tools(
           server,
           models=[MenuItem, OrderLine],
           using="mcp_reader",
           context="MCPizza's sales data. Prices and totals are in pounds (GBP).",
       )

   ``context`` is optional text at the top of the table list, for anything that applies across tables.
   See :func:`add_sql_tools` for the other options.

In tests, the mirrored alias is still a separate connection, which sees only committed data, so test the tools with ``TransactionTestCase`` rather than ``TestCase``.

What the model sees
-------------------

``sql_schema`` works in two steps, so a large schema doesn’t fill the model’s context up front.
Called without arguments, it lists the tables, each with its description from the model.
For the example project:

.. code-block:: text

    MCPizza's sales data. Prices and totals are in pounds (GBP).

    Queries run with the time zone UTC, which applies to date functions on timestamp with time zone columns.

    Tables:
    - pizzeria_report_menu_item: Every pizza MCPizza has sold, including specials no longer on the menu.
    - pizzeria_report_order_line: One row per order. The customer’s free-text requests are not exposed, only whether there were any.

    Call sql_schema with tables to see their columns.

The model then calls it again with the tables it needs, such as ``{"tables": ["pizzeria_report_order_line"]}``, and gets their columns as SQL, the format models know best for describing tables:

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

So the table descriptions are the model’s guide to which tables to look at: keep them short, but say what a row is.

Views are shown as tables too, since the difference doesn’t matter for reading them.

The time zone line in the table list comes from Django: with |USE_TZ|__ enabled, connections use UTC.
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

* Read the system catalogs, such as ``pg_class`` and ``pg_attribute``, which list every table and column, though not their data, ``pg_views``, which shows the definitions of views, and ``pg_roles``, which lists role names.
  It can’t read the data of tables and columns it wasn’t granted, or other roles’ queries in ``pg_stat_activity``.

* See other sessions of the same role in ``pg_stat_activity``, including their SQL, and cancel or terminate them.
  Since every caller of the tools shares the role, one could see another’s queries, which may quote their conversation.
  To hide them, a superuser can run ``ALTER ROLE mcp_reader SET track_activities = off``.

* Take advisory locks, which are database-wide, for up to the timeout, which could block your application if it uses them.

* Use CPU, memory, and I/O for roughly up to twice the timeout, per call, as the query and fetching the rows are timed separately.
  A row is transferred in full, so a query returning a huge value, such as from ``repeat('x', 100000000)``, uses that much memory in your Django process before it is shortened.

Use your server’s ``auth`` and the ``permission`` argument to limit who can call the tools, and consider pointing the alias at a read replica.

The tools are marked as read-only, with ``readOnlyHint``, so clients may run them without asking the user each time.
