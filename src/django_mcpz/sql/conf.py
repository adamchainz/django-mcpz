"""
The MCPZ_SQL_READERS setting, which says what each read-only database role
can see, for both the tools and the grants that enforce it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Field, Model

SETTING = "MCPZ_SQL_READERS"


@dataclass(frozen=True)
class Table:
    """A model's table, and the fields exposed as its columns."""

    model: type[Model]
    fields: tuple[Field[Any, Any], ...]

    @property
    def name(self) -> str:
        return self.model._meta.db_table

    @property
    def columns(self) -> list[str]:
        return [field.column for field in self.fields if field.column]


@dataclass(frozen=True)
class Reader:
    """
    A read-only role: the database alias that logs in as it, the alias that
    maintains its grants, and the tables it can read.
    """

    alias: str
    using: str
    tables: tuple[Table, ...]


def get_readers() -> list[Reader]:
    """Every reader in the setting, checked."""
    config = getattr(settings, SETTING, {})
    if not isinstance(config, Mapping):
        raise ImproperlyConfigured(f"{SETTING} must be a dict, not {config!r}.")
    return [_reader(alias, config[alias]) for alias in config]


def get_reader(alias: str) -> Reader:
    """The reader for the database alias, checked."""
    config = getattr(settings, SETTING, {})
    if not isinstance(config, Mapping) or alias not in config:
        raise ImproperlyConfigured(
            f"{SETTING} has no reader for the database alias {alias!r}."
        )
    return _reader(alias, config[alias])


def _reader(alias: str, config: Any) -> Reader:
    where = f"{SETTING}[{alias!r}]"
    if alias not in settings.DATABASES:
        raise ImproperlyConfigured(
            f"{where} needs {alias!r} to be a database alias in DATABASES,"
            " which logs in as the reader role."
        )
    if not isinstance(config, Mapping):
        raise ImproperlyConfigured(f"{where} must be a dict, not {config!r}.")
    unknown = sorted(set(config) - {"tables", "using"})
    if unknown:
        raise ImproperlyConfigured(
            f"{where} has unknown keys: {', '.join(map(repr, unknown))}."
        )
    using = config.get("using", "default")
    if using not in settings.DATABASES or using == alias:
        raise ImproperlyConfigured(
            f"{where}['using'] must be another database alias in DATABASES,"
            f" whose role maintains the reader's grants, not {using!r}."
        )
    tables = resolve_tables(config.get("tables"), connections[using], where)
    return Reader(alias=alias, using=using, tables=tuple(tables))


def resolve_tables(
    tables: Any, connection: BaseDatabaseWrapper, where: str
) -> list[Table]:
    """
    Check a dict of model labels to field names, returning each model's
    fields in the model's order. Every field must be named, so adding a field
    to a model never exposes its column.
    """
    if not isinstance(tables, Mapping) or not tables:
        raise ImproperlyConfigured(
            f"{where}['tables'] must be a dict of model labels, like"
            " 'app_label.ModelName', to lists of field names."
        )
    result: list[Table] = []
    names: set[str] = set()
    for label, field_names in tables.items():
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            raise ImproperlyConfigured(
                f"{where}['tables'] has {label!r}, which isn't an installed"
                " model's label."
            ) from None
        opts = model._meta
        if isinstance(field_names, str) or not field_names:
            raise ImproperlyConfigured(
                f"{where}['tables'][{label!r}] must be a list of field names,"
                f" not {field_names!r}."
            )
        if opts.db_table in names:
            raise ImproperlyConfigured(
                f"{where}['tables'] has the table {opts.db_table!r} more than"
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
                    f"{where}['tables'][{label!r}] has {field_name!r}, which"
                    f" isn't a field of {opts.label}."
                ) from None
            name = str(found.name)
            if columns.get(name) is not found:
                raise ImproperlyConfigured(
                    f"{where}['tables'][{label!r}] has {field_name!r}, which"
                    f" isn't a column of {opts.label}'s table."
                )
            if name in chosen:
                raise ImproperlyConfigured(
                    f"{where}['tables'][{label!r}] has {field_name!r} more than once."
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
