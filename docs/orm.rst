ORM query tool
==============

.. currentmodule:: django_mcpz.orm

Instead of writing a tool per question, you can let the calling model write read-only Django ORM queries itself.
:func:`add_query_tool` registers one tool that takes Python code like this, runs it, and returns the rows:

.. code-block:: python

    from shop.models import Order
    from django.db.models import Count

    result = (
        Order.objects.filter(status="shipped")
        .values("country")
        .annotate(n=Count("id"))
        .order_by("-n")[:10]
    )

Register it on a server with the models it may read:

.. code-block:: python

    from django_mcpz.orm import add_query_tool
    from django_mcpz.server import MCPServer
    from django_mcpz.bearer_tokens.auth import token_auth

    server = MCPServer(name="shop", version="1.0.0", auth=token_auth)

    add_query_tool(server, models=["shop", "auth.User"])

``models`` takes app labels, model labels, and model classes.
Nothing outside that list can be queried, by import or by following a relation.

The tool, named ``query`` by default, has two optional arguments, ``query`` and ``app``.
Called with neither, it lists the apps and models the caller may read.
Called with ``app``, it lists that app’s models, with the import line for each, its managers, and its fields, with their types, relations, choices, and ``help_text``.
Called with ``query``, it runs the code and returns the rows as Python literals, like ``[{'country': 'GB', 'n': 12}, ...]``.
The tool’s description tells the model all this, so a client needs no other instructions.

How queries are run
-------------------

The code is never passed to ``exec()`` or ``eval()``.
It is parsed with |ast|__ and evaluated by a small interpreter that allows only ``from module import name`` statements, assignments, literals, calls, slices, and the operators expressions use, such as ``|`` on ``Q`` objects and ``*`` on ``F`` objects.
Each attribute access and call is checked against the object it runs on:

.. |ast| replace:: ``ast``
__ https://docs.python.org/3/library/ast.html

* Model classes expose only their managers, such as ``objects``.
* Managers and querysets expose only read-only methods: ``all``, ``filter``, ``exclude``, ``annotate``, ``alias``, ``order_by``, ``reverse``, ``distinct``, ``values``, ``values_list``, ``dates``, ``datetimes``, ``none``, ``union``, ``intersection``, ``difference``, ``get``, ``first``, ``last``, ``latest``, ``earliest``, ``count``, ``exists``, and ``aggregate``.
  Writes, ``raw()``, ``extra()``, and custom manager or queryset methods are refused.
* Expressions expose ``asc()`` and ``desc()``.
* Imports are limited to the allowed models, and from Django, ``F``, ``Q``, ``Value``, ``OuterRef``, ``Subquery``, ``Exists``, ``Case``, ``When``, ``ExpressionWrapper``, ``Window``, the aggregates, the field classes for ``output_field``, and the concrete `database functions <https://docs.djangoproject.com/en/stable/ref/models/database-functions/>`__, plus the PostgreSQL aggregates and search expressions where available.
  The standard library’s ``date``, ``datetime``, ``time``, ``timedelta``, ``Decimal``, and ``UUID`` are allowed too, as is ``django.utils.timezone.now``.
  The base classes that take SQL as an argument, such as ``Func`` and ``RawSQL``, are not, and every call refuses the ``function``, ``template``, and ``arg_joiner`` arguments that would inject SQL through a function subclass.

The query must end by assigning to ``result``, or with a bare expression, and that value must be a queryset ending in ``values()`` or ``values_list()``, or the value of ``count()``, ``exists()``, ``aggregate()``, ``first()``, ``last()``, or ``get()`` on one.
Model instances are never returned, so the model has to name the fields it wants.

Every refusal is an in-band error naming the line and what is allowed instead, so the calling model can correct itself.
Errors from Django and the database, such as a ``FieldError`` for a misspelt field, are relayed the same way.

Permissions
-----------

Two checks apply on each call.

First, the tool’s own ``permission``, as for any tool: by default, callers who may read none of the models do not see the tool at all.

Second, ``model_permission`` decides per model, per request.
By default it checks Django’s ``view`` permission for the model, ``<app_label>.view_<model_name>``, with |has_perm|__, so the models a caller sees in the listings and may import in queries follow the permissions of the user behind their token.
This needs ``request.user``, which the bearer tokens and OAuth apps set; without it the check raises ``ImproperlyConfigured``.
Pass a callable taking the request and the model class for another policy, such as allowing everything on a server that authenticates some other way:

.. |has_perm| replace:: ``has_perm()``
__ https://docs.djangoproject.com/en/stable/ref/contrib/auth/#django.contrib.auth.models.User.has_perm

.. code-block:: python

    add_query_tool(
        server,
        models=["shop"],
        model_permission=lambda request, model: True,
    )

Relations are checked too, since ``values("customer__email")`` would otherwise read a model the caller cannot import.
Before any SQL runs, the query is compiled and every table it joins is checked, including joins from ``order_by()``, from a model’s default ``ordering``, from subqueries, and from combined queries.
A query touching a model the caller may not read is refused with a message naming it.
Automatic many-to-many tables come with the model that defines the field, and the parent tables of multi-table inheritance children come with the child.

Hidden fields
-------------

``hidden_fields`` names fields that queries may not read, select, filter, or order by, as ``"app_label.Model.field"`` strings.
They are left out of the field listings, and a query naming one is refused, including through relations.
The ``password`` field of any user model, meaning a subclass of ``AbstractBaseUser``, is always hidden.

.. code-block:: python

    add_query_tool(
        server,
        models=["shop"],
        hidden_fields=["shop.Customer.card_number"],
    )

Queries that would select every field, such as ``values()`` without arguments, are refused on models with hidden fields, so the model has to name the fields it wants.

.. _orm-timeouts:

Timeouts and limits
-------------------

``timeout``, 30 seconds by default, limits how long each query may run, so a heavy join or a wide scan cannot tie up a database connection.
It is enforced by the database: on PostgreSQL with ``statement_timeout``, set locally within a transaction; on MySQL with ``max_execution_time`` and MariaDB with ``max_statement_time``; and on SQLite with a progress handler that interrupts the statement.
Other databases run without a timeout.
A query that times out is cancelled and reported to the model as an error suggesting it narrow the query.

``max_rows``, 200 by default, caps the rows returned from a query.
The result notes when a query returned more, and the model can page through by slicing, as in ``[200:400]``.

``using`` runs every query on the named database alias, such as a read replica, instead of the one the database routers choose.

Database functions can still be expensive, and a caller with a token can send as many queries as they like, so treat the tool like any other read-only API: put it behind authentication and consider a replica for heavy use.

Docstrings
----------

Pass ``docstrings=True`` to include each model’s docstring in the app listings, where it may help the model understand what a table holds.
Django’s generated docstrings, like ``Order(id, status, total)``, are skipped, and an inherited docstring is used when the model itself has none.
Docstrings are written for developers, so check they read well before exposing them.

Other options
-------------

``name`` and ``title`` name the tool, and ``instructions`` adds text to the end of its description, for guidance specific to your data such as the meanings of status codes.

Register the tool more than once, with different names and models, for tools with different scopes.
