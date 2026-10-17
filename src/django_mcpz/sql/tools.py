"""
Tools that let an LLM run read-only SQL queries, limited to the chosen columns
of tables and views of an SQLReader.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Annotated, Any

import msgspec
import msgspec.json
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.utils import CursorWrapper
from django.db.models import CompositePrimaryKey, Field, Model
from django.http import HttpRequest
from django_msgspec import enc_hook as django_enc_hook

from django_mcpz.server import MCPServer, ToolError
from django_mcpz.sql.queries import get_query
from django_mcpz.sql.readers import SQLReader, Table, db_type, quote_name

# Rows are fetched in batches, checking the limits between them, so a query
# with huge values can't fill the server's memory. The first batch is one
# row, and later ones are sized from the rows so far, up to this.
MAX_FETCH_BATCH = 100


class SQLSchemaParams(msgspec.Struct):
    tables: Annotated[
        list[str] | None,
        msgspec.Meta(
            description=(
                "Tables to describe the columns of. Leave out to list the"
                " tables instead."
            )
        ),
    ] = None


class SQLQueryParams(msgspec.Struct):
    sql: Annotated[
        str,
        msgspec.Meta(
            description=(
                "A single SELECT statement. WITH, VALUES, and TABLE work"
                " too. No other statement types."
            )
        ),
    ]


def add_sql_tools(
    server: MCPServer,
    *,
    reader: SQLReader,
    context: str | None = None,
    max_rows: int = 1000,
    max_size: int = 50_000,
    timeout: float = 10.0,
    name_prefix: str = "sql",
    permission: Callable[[HttpRequest], bool] | str | None = None,
) -> None:
    """
    Add two tools to the server: one listing the reader's tables, or
    describing the columns of some, as SQL, and one running a read-only query
    against them.

    Queries run through the reader's database alias. On PostgreSQL, that
    logs in as a role that may only SELECT the reader's columns, as
    maintained by the django_mcpz.sql app after every migrate, and the role
    is the security boundary. On SQLite, an authorizer callback allows only
    reads of those columns. Both add a single statement per call, a timeout,
    and limits on the result.
    """
    if not apps.is_installed("django_mcpz.sql"):
        raise ImproperlyConfigured(
            "add_sql_tools() needs 'django_mcpz.sql' in INSTALLED_APPS, to"
            " maintain the reader role's grants."
        )
    using = reader.using
    connection = connections[using]
    get_query(connection)
    if connection.vendor == "postgresql":
        try:
            import psycopg  # noqa: F401
        except ImportError:  # pragma: no cover
            raise ImproperlyConfigured("The SQL tools need psycopg 3.") from None
        if connection.settings_dict["OPTIONS"].get("assume_role"):
            raise ImproperlyConfigured(
                f"add_sql_tools() needs {using!r} to log in as the reader role,"
                " not use assume_role, since queries could switch back to the"
                " login role with set_config('role', ...)."
            )
    if max_rows < 1:
        raise ImproperlyConfigured("max_rows must be at least 1.")
    if max_size < 1000:
        raise ImproperlyConfigured("max_size must be at least 1000.")
    if timeout <= 0:
        raise ImproperlyConfigured("timeout must be positive.")

    schema_name = f"{name_prefix}_schema"
    query_name = f"{name_prefix}_query"

    @server.tool(
        name=schema_name,
        description=(
            f"Without tables, list the tables {query_name} can query, with"
            " what they hold. With tables, describe their columns, as CREATE"
            " TABLE statements with comments explaining the data. Call this"
            " before writing queries."
        ),
        read_only=True,
        idempotent=True,
        open_world=False,
        permission=permission,
    )
    def sql_schema(request: HttpRequest, params: SQLSchemaParams) -> str:
        if not params.tables:
            return list_tables(
                reader.tables, using=using, context=context, schema_tool=schema_name
            )
        by_name = {table.name: table for table in reader.tables}
        # Accept names as the model would write them in SQL, quoted or not.
        names = [name.strip().strip('"').replace('""', '"') for name in params.tables]
        unknown = [name for name in names if name not in by_name]
        if unknown:
            raise ToolError(
                f"Unknown tables: {', '.join(quote_name(n) for n in unknown)}."
                " Available tables:"
                f" {', '.join(quote_name(n) for n in by_name)}."
            )
        return describe_tables(
            [by_name[name] for name in dict.fromkeys(names)],
            using=using,
            all_tables=reader.tables,
        )

    @server.tool(
        name=query_name,
        description=(
            f"Run one read-only {connection.display_name} SELECT query and get the"
            " result rows,"
            ' as JSON: "columns" names the columns, and "rows" holds one array'
            f" of values per row. Call {schema_name} to find the tables and"
            f" their columns first. At most {max_rows} rows are returned, and"
            " large results are cut short, so aggregate, filter, and sort in"
            " SQL rather than fetching everything. Queries time out after"
            f" {timeout:g} seconds."
        ),
        read_only=True,
        idempotent=True,
        open_world=False,
        permission=permission,
    )
    def sql_query(request: HttpRequest, params: SQLQueryParams) -> msgspec.Raw:
        columns, rows, more = run_query(
            params.sql,
            reader=reader,
            max_rows=max_rows,
            max_size=max_size,
            timeout=timeout,
        )
        return encode_result(columns, rows, more=more, max_size=max_size)


def run_query(
    sql: str, *, reader: SQLReader, max_rows: int, max_size: int, timeout: float
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """
    Run a query as the reader, returning the column names, up to max_rows
    rows, and whether rows were left unfetched. It runs in a transaction, or
    savepoint, that is always rolled back, made read-only by the database's
    query context manager.

    Rows are fetched in batches, stopping at max_rows, once roughly max_size
    bytes of values have arrived, or once the timeout has passed, since on
    PostgreSQL, each FETCH gets its own statement timeout.
    """
    connection = connections[reader.using]
    query = get_query(connection)
    sql = sql.strip().rstrip(";").rstrip()
    if not sql:
        raise ToolError("The SQL is empty.")
    deadline = time.monotonic() + timeout
    with query(connection, reader, sql, timeout) as cursor:
        columns = [column[0] for column in cursor.description]
        rows, more = _fetch(cursor, max_rows, max_size, deadline)
    return columns, rows, more


def _fetch(
    cursor: CursorWrapper, max_rows: int, max_size: int, deadline: float
) -> tuple[list[tuple[Any, ...]], bool]:
    rows: list[tuple[Any, ...]] = []
    size = 0
    batch_size = 1
    while True:
        batch = cursor.fetchmany(min(batch_size, max_rows + 1 - len(rows)))
        rows.extend(batch)
        if len(rows) > max_rows:
            return rows[:max_rows], True
        if not batch:
            return rows, False
        size += sum(_rough_size(value) for row in batch for value in row)
        if size > max_size or time.monotonic() > deadline:
            # Probably more, which would cost a FETCH to find out.
            return rows, True
        # As many rows as probably fit in what's left of max_size.
        row_size = size // len(rows) + 1
        batch_size = max(1, min(MAX_FETCH_BATCH, (max_size - size) // row_size))


def _rough_size(value: Any) -> int:
    if isinstance(value, (str, bytes)):
        return len(value)
    return 8


# Results


def _encode_hook(value: Any) -> Any:
    try:
        return django_enc_hook(value)
    except TypeError:
        # Ranges, network addresses, and other PostgreSQL types, in their
        # text form.
        return str(value)


_encoder = msgspec.json.Encoder(enc_hook=_encode_hook, decimal_format="number")


def encode_result(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    more: bool,
    max_size: int,
) -> msgspec.Raw:
    """
    Encode a query result as JSON, with column names once and rows as arrays,
    which is unambiguous about types and nulls, yet nearly as compact as CSV.

    The result is kept to roughly max_size bytes, so it fits in a model's
    context: long values are shortened to a fair share of it, and rows that
    do not fit are dropped, with a note saying so.
    """
    max_value = max_size // max(10, len(columns))
    budget = max_size - len(_encoder.encode(columns)) - 200
    kept: list[list[Any]] = []
    shortened = False
    for row in rows:
        values = []
        for value in row:
            value, was_shortened = _shorten(_finite(value), max_value)
            shortened = shortened or was_shortened
            values.append(value)
        budget -= len(_encoder.encode(values)) + 1
        if budget < 0 and kept:
            more = True
            break
        kept.append(values)
    result: dict[str, Any] = {"columns": columns, "rows": kept}
    notes = []
    if more:
        notes.append(
            f"Only the first {len(kept)} rows are shown. Aggregate, filter,"
            " or add a LIMIT to see the rest."
        )
    if shortened:
        notes.append(
            "Long values were shortened. Select parts of them, such as with"
            " substr(), to read more."
        )
    if notes:
        result["truncated"] = True
        result["note"] = " ".join(notes)
    # Encoded here, rather than by the server, for decimals as numbers.
    return msgspec.Raw(_encoder.encode(result))


def _finite(value: Any) -> Any:
    """
    NaN and infinities as strings, since JSON has no numbers for them.
    msgspec would encode float ones as null, and decimal ones as invalid JSON.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(Decimal(value))
    if isinstance(value, Decimal) and not value.is_finite():
        return str(value)
    return value


def _shorten(value: Any, max_value: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float, Decimal)):
        return value, False
    else:
        # Arrays, JSON, bytes, and so on, by their encoded size.
        text = _encoder.encode(value).decode()
    if len(text) <= max_value:
        return value, False
    return f"{text[:max_value]}… [{len(text) - max_value} more characters]", True


# Schema description


def list_tables(
    table_list: Sequence[Table],
    *,
    using: str,
    context: str | None = None,
    schema_tool: str = "sql_schema",
) -> str:
    """
    List the tables, each with its description, from the model's
    db_table_comment or docstring, plus the context and anything else that
    applies to every query.
    """
    connection = connections[using]
    parts = []
    if context:
        parts.append(context.strip())
    if connection.vendor == "sqlite":
        parts.append(
            "Dates and times are stored as ISO 8601 text, in UTC, so use"
            " SQLite's date and time functions on them."
        )
    else:
        parts.append(
            f"Queries run with the time zone {connection.timezone_name}, which"
            " applies to date functions on timestamp with time zone columns."
        )
    lines = ["Tables:"]
    for table in table_list:
        line = f"- {quote_name(table.name)}"
        description = _table_description(table.model)
        if description:
            line += f": {' '.join(description.split())}"
        lines.append(line)
    parts.append("\n".join(lines))
    parts.append(f"Call {schema_tool} with tables to see their columns.")
    return "\n\n".join(parts) + "\n"


def describe_tables(
    table_list: Sequence[Table],
    *,
    using: str,
    all_tables: Sequence[Table] | None = None,
) -> str:
    """
    Describe the tables as CREATE TABLE statements, with comments from each
    model's db_table_comment or docstring, and each field's db_comment or
    help_text, verbose name, and choices. Foreign keys to exposed columns of
    all_tables, by default the same tables, get REFERENCES.
    """
    connection = connections[using]
    exposed = {table.name: set(table.columns) for table in (all_tables or table_list)}
    parts = [_describe_table(table, connection, exposed) for table in table_list]
    return "\n\n".join(parts) + "\n"


def _table_description(model: type[Model]) -> str:
    return model._meta.db_table_comment or _docstring(model)


def _describe_table(
    table: Table, connection: BaseDatabaseWrapper, exposed: dict[str, set[str]]
) -> str:
    opts = table.model._meta
    lines = []
    description = _table_description(table.model)
    if description:
        lines.append(_comment(description))
    lines.append(f"CREATE TABLE {quote_name(opts.db_table)} (")
    columns = []
    for field in table.fields:
        column = f"  {_quote_column(field)} {db_type(field, connection)}"
        if not field.null:
            column += " NOT NULL"
        if field.primary_key:
            column += " PRIMARY KEY"
        target = field.target_field if field.is_relation else None  # type: ignore[attr-defined]
        if target is not None and target.column in exposed.get(
            target.model._meta.db_table, ()
        ):
            column += (
                f" REFERENCES {quote_name(target.model._meta.db_table)}"
                f" ({_quote_column(target)})"
            )
        columns.append((column, _field_notes(field)))
    if isinstance(opts.pk, CompositePrimaryKey) and all(
        field in table.fields for field in opts.pk.fields
    ):
        pk_columns = ", ".join(_quote_column(field) for field in opts.pk.fields)
        columns.append((f"  PRIMARY KEY ({pk_columns})", ""))
    for i, (column, notes) in enumerate(columns):
        if i < len(columns) - 1:
            column += ","
        if notes:
            column += f"  -- {notes}"
        lines.append(column)
    lines.append(");")
    return "\n".join(lines)


def _quote_column(field: Any) -> str:
    return quote_name(field.column)


def _docstring(model: type[Model]) -> str:
    doc = model.__doc__ or ""
    # Django generates a docstring listing the fields for models without one,
    # which says nothing the CREATE TABLE does not.
    if doc.startswith(f"{model.__name__}("):
        return ""
    return " ".join(doc.split())


def _field_notes(field: Field[Any, Any]) -> str:
    notes = []
    verbose_name = str(field.verbose_name)
    if verbose_name and verbose_name.lower() != field.name.replace("_", " ").lower():
        notes.append(verbose_name[0].upper() + verbose_name[1:] + ".")
    description = field.db_comment or str(field.help_text)
    if description:
        notes.append(description)
    if field.choices:
        choices = ", ".join(
            f"{_sql_literal(value)} = {label}" for value, label in field.flatchoices
        )
        notes.append(f"Values: {choices}.")
    return " ".join(" ".join(note.split()) for note in notes)


def _sql_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _comment(text: str) -> str:
    return "\n".join(f"-- {line}".rstrip() for line in text.strip().splitlines())
