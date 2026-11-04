"""
What the SQL tools can read, defined with SQLReader alongside the tools: the
outer limit on every query, of tables and their columns.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from django.conf import settings
from django.contrib.auth import get_permission_codename
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Field, Model
from django.http import HttpRequest

from django_mcpz.sql.queries import get_query


class Table:
    """A model's table, and the fields exposed as its columns."""

    __slots__ = ("model", "fields")

    model: type[Model]
    fields: tuple[Field[Any, Any], ...]

    def __init__(self, model: type[Model], fields: tuple[Field[Any, Any], ...]):
        self.model = model
        self.fields = fields

    @property
    def name(self) -> str:
        return self.model._meta.db_table

    @property
    def columns(self) -> list[str]:
        return [field.column for field in self.fields if field.column]


class SQLReader:
    """
    What the SQL tools can read: the columns of the models' tables, through
    the database alias using. Every field must be named, so adding a field
    to a model never exposes its column. The tools check each query reads
    only these, before running it.

    With view_permissions, each request sees only the tables whose models
    its user has the view permission for.
    """

    __slots__ = ("using", "tables", "view_permissions")

    using: str
    tables: tuple[Table, ...]
    view_permissions: bool

    def __init__(
        self,
        *,
        tables: Mapping[type[Model], Sequence[str]],
        using: str = "default",
        view_permissions: bool = False,
    ) -> None:
        if using not in settings.DATABASES:
            raise ImproperlyConfigured(
                f"SQLReader using={using!r} isn't a database alias in DATABASES."
            )
        connection = connections[using]
        get_query(connection)
        if connection.vendor == "postgresql":
            try:
                import psycopg  # noqa: F401
            except ImportError:  # pragma: no cover
                raise ImproperlyConfigured("The SQL tools need psycopg 3.") from None
        object.__setattr__(self, "using", using)
        object.__setattr__(self, "tables", tuple(_resolve_tables(tables, connection)))
        object.__setattr__(self, "view_permissions", view_permissions)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SQLReader is immutable.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("SQLReader is immutable.")

    def visible_tables(self, request: HttpRequest) -> list[Table]:
        """
        The tables the request may see: all of them, or with
        view_permissions, those whose models its user has the view
        permission for, as the admin checks.
        """
        if not self.view_permissions:
            return list(self.tables)
        return [
            table
            for table in self.tables
            if request.user.has_perm(
                f"{table.model._meta.app_label}."
                f"{get_permission_codename('view', table.model._meta)}"
            )
        ]


def _resolve_tables(tables: Any, connection: BaseDatabaseWrapper) -> list[Table]:
    """
    Check a dict of models to field names, returning each model's fields in
    the model's order. Typed loosely, since it checks the types too.
    """
    if not isinstance(tables, Mapping) or not tables:
        raise ImproperlyConfigured(
            "SQLReader tables must be a dict of models to lists of field names."
        )
    result: list[Table] = []
    names: set[str] = set()
    for model, field_names in tables.items():
        if not isinstance(model, type) or not issubclass(model, Model):
            raise ImproperlyConfigured(
                f"SQLReader tables must have models as keys, not {model!r}."
            )
        opts = model._meta
        where = f"SQLReader tables[{opts.label}]"
        if isinstance(field_names, str) or not field_names:
            raise ImproperlyConfigured(
                f"{where} must be a list of field names, not {field_names!r}."
            )
        if opts.db_table in names:
            raise ImproperlyConfigured(
                f"SQLReader tables has the table {opts.db_table!r} more than"
                " once. List all its fields together."
            )
        names.add(opts.db_table)
        # Local fields only, since a multi-table inheritance child's table
        # holds only its own columns, plus the link to its parent.
        columns = {
            field.name: field
            for field in opts.local_concrete_fields
            if db_type(field, connection) is not None
        }
        chosen: set[str] = set()
        for field_name in field_names:
            try:
                found = opts.get_field(field_name)
            except FieldDoesNotExist:
                raise ImproperlyConfigured(
                    f"{where} has {field_name!r}, which isn't a field of {opts.label}."
                ) from None
            name = str(found.name)
            if columns.get(name) is not found:
                raise ImproperlyConfigured(
                    f"{where} has {field_name!r}, which isn't a column of"
                    f" {opts.label}'s table."
                )
            if name in chosen:
                raise ImproperlyConfigured(
                    f"{where} has {field_name!r} more than once."
                )
            chosen.add(name)
        fields = tuple(
            field for column_name, field in columns.items() if column_name in chosen
        )
        result.append(Table(model, fields))
    return result


def db_type(field: Field[Any, Any], connection: BaseDatabaseWrapper) -> str | None:
    if field.is_relation:
        return field.target_field.rel_db_type(connection)  # type: ignore[attr-defined,no-any-return]
    return field.db_type(connection)


# PostgreSQL's reserved key words, which can't be identifiers unquoted.
_RESERVED = frozenset(
    [
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "authorization",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "collation",
        "column",
        "concurrently",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "system_user",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    ]
)
_PLAIN_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*")


def quote_name(name: str) -> str:
    """
    Quote an identifier only when it needs quoting, keeping the schema
    readable, and the model's SQL natural.
    """
    if _PLAIN_IDENTIFIER.fullmatch(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'
