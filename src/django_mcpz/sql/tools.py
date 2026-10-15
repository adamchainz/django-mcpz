"""
Tools that let an LLM run read-only SQL queries against PostgreSQL, as the role
of an SQLReader, limited to chosen columns of chosen tables and views.
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
from django.db import connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import CompositePrimaryKey, Field, Model
from django.http import HttpRequest
from django_msgspec import enc_hook as django_enc_hook

from django_mcpz.server import MCPServer, ToolError
from django_mcpz.sql.readers import SQLReader, Table, db_type, quote_name

try:
    import psycopg
    import psycopg.pq
    from psycopg.types.json import JsonbLoader
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

# The name of the server-side cursor each query runs through.
CURSOR_NAME = "mcp_query"
_DECLARE = f'DECLARE "{CURSOR_NAME}" CURSOR FOR '

# SQLSTATE classes of errors in the query itself, which the model can fix.
# Others, such as connection failures (08) and running out of resources
# (53), are the server's problem, and raised as such.
_QUERY_ERROR_CLASSES = frozenset(
    {
        "0A",  # feature not supported
        "21",  # cardinality violation
        "22",  # data exception
        "25",  # invalid transaction state, such as writing when read-only
        "2F",  # SQL routine exception
        "38",  # external routine exception
        "39",  # external routine invocation exception
        "42",  # syntax error or access rule violation
        "54",  # program limit exceeded
        "P0",  # PL/pgSQL error
    }
)
_QUERY_CANCELED = "57014"
_SYNTAX_ERROR = "42601"

# Rows are fetched in batches, checking the limits between them, so a query
# with huge values can't fill the server's memory. The first batch is one
# row, and later ones are sized from the rows so far, up to this.
MAX_FETCH_BATCH = 100

# Checked on every query, alongside setting the timeout: the login role must
# be a plain one, unable to read more than it was granted.
_SETUP_SQL = """
SELECT
    set_config('statement_timeout', %s, true),
    current_user <> session_user
    OR rolsuper
    OR rolbypassrls
    OR EXISTS (SELECT FROM pg_auth_members WHERE member = pg_roles.oid)
FROM pg_roles
WHERE rolname = session_user
"""

_ONLY_SELECT = "Only a single SELECT statement is allowed, or WITH, VALUES, or TABLE."


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
                "A single PostgreSQL SELECT statement. WITH, VALUES, and"
                " TABLE work too. No other statement types."
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

    Queries run through the reader's database alias, which logs in as a
    PostgreSQL role that may only SELECT the reader's columns, as maintained
    by the django_mcpz.sql app after every migrate. That role is the security
    boundary. This adds read-only transactions, a single
    statement per call, a statement timeout, and limits on the result.
    """
    if not apps.is_installed("django_mcpz.sql"):
        raise ImproperlyConfigured(
            "add_sql_tools() needs 'django_mcpz.sql' in INSTALLED_APPS, to"
            " maintain the reader role's grants."
        )
    using = reader.using
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise ImproperlyConfigured(
            f"add_sql_tools() needs a PostgreSQL database, but {using!r} is"
            f" {connection.vendor}."
        )
    if connection.settings_dict["OPTIONS"].get("assume_role"):
        raise ImproperlyConfigured(
            f"add_sql_tools() needs {using!r} to log in as the reader role,"
            " not use assume_role, since queries could switch back to the"
            " login role with set_config('role', ...)."
        )
    if psycopg is None:  # pragma: no cover
        raise ImproperlyConfigured("add_sql_tools() needs psycopg 3.")
    if max_rows < 1:
        raise ImproperlyConfigured("max_rows must be at least 1.")
    if max_size < 1000:
        raise ImproperlyConfigured("max_size must be at least 1000.")
    if timeout <= 0:
        raise ImproperlyConfigured("timeout must be positive.")

    # The tools refer to the reader, keeping it alive in the registry that
    # the post_migrate handler syncs.
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
            "Run one read-only PostgreSQL SELECT query and get the result rows,"
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
            using=using,
            max_rows=max_rows,
            max_size=max_size,
            timeout=timeout,
        )
        return encode_result(columns, rows, more=more, max_size=max_size)


def run_query(
    sql: str, *, using: str, max_rows: int, max_size: int, timeout: float
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """
    Run a query in a read-only transaction, returning the column names, up to
    max_rows rows, and whether rows were left unfetched.

    The query runs through a server-side cursor, so PostgreSQL wraps it as
    DECLARE ... CURSOR FOR <sql>, sent with the extended query protocol. That
    accepts exactly one statement, which must be a query: no data-modifying
    statements, even in a WITH clause, and no transaction control.

    Rows are fetched in batches, stopping at max_rows, once roughly max_size
    bytes of values have arrived, or once the timeout has passed, since each
    FETCH gets its own statement timeout.
    """
    connection = connections[using]
    sql = sql.strip().rstrip(";").rstrip()
    if not sql:
        raise ToolError("The SQL is empty.")
    deadline = time.monotonic() + timeout
    try:
        with transaction.atomic(using=using):
            # Never commit: there is nothing to commit, and anything that
            # slips past the read-only transaction is undone. Rolling back
            # also reverts settings changed with set_config().
            connection.set_rollback(True)
            connection.ensure_connection()
            pg_connection = connection.connection
            with pg_connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(_SETUP_SQL, [f"{max(1, int(timeout * 1000))}ms"])
                privileged = cursor.fetchone()[1]
            if privileged:
                raise ImproperlyConfigured(
                    f"The database alias {using!r} must log in as a role that"
                    " is not a superuser, cannot bypass row-level security,"
                    " and is not a member of other roles, or queries could"
                    " read beyond the role's grants."
                )
            with psycopg.ServerCursor(pg_connection, CURSOR_NAME) as cursor:
                # Django loads jsonb as text, for JSONField. Load it as JSON
                # here, so it's nested JSON in results.
                cursor.adapters.register_loader("jsonb", JsonbLoader)
                # A newline ends any trailing -- comment, before the rest of
                # the statement psycopg sends.
                cursor.execute(f"{sql}\n")
                assert cursor.description is not None
                columns = [column.name for column in cursor.description]
                rows, more = _fetch(cursor, max_rows, max_size, deadline)
    except psycopg.Error as exc:
        raise _query_error(exc, sql, timeout) from None
    finally:
        _release_advisory_locks(connection)
    return columns, rows, more


def _fetch(
    cursor: psycopg.ServerCursor[Any], max_rows: int, max_size: int, deadline: float
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


def _release_advisory_locks(connection: BaseDatabaseWrapper) -> None:
    """
    Release session-level advisory locks the query took, with functions like
    pg_advisory_lock(). Unlike settings, they outlast the rollback, and would
    be held by the connection for later requests.
    """
    pg_connection = connection.connection
    if (
        pg_connection is None
        or pg_connection.closed
        # In an outer transaction, which failed.
        or pg_connection.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
    ):
        return
    with pg_connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock_all()")


def _query_error(exc: psycopg.Error, sql: str, timeout: float) -> Exception:
    sqlstate = exc.sqlstate
    if sqlstate == _QUERY_CANCELED:
        return ToolError(
            f"The query was cancelled after running for {timeout:g} seconds."
            " Make it cheaper, for example by filtering earlier or"
            " aggregating less data."
        )
    if sqlstate is None and isinstance(exc, psycopg.DataError):
        # A value psycopg can't load, such as an infinite or BC date.
        return ToolError(
            f"Query error: {exc}. Cast the column to text to read such values."
        )
    if sqlstate is None or sqlstate[:2] not in _QUERY_ERROR_CLASSES:
        return exc
    message = _strip_declare(str(exc))
    if sqlstate == _SYNTAX_ERROR and (
        "multiple commands" in message or not _looks_like_query(sql)
    ):
        message += f"\n{_ONLY_SELECT}"
    return ToolError(f"Query error: {message}")


def _strip_declare(message: str) -> str:
    """
    Remove the DECLARE that wraps the query from PostgreSQL's quote of it in
    errors, moving the caret that marks the error position to match.
    """
    lines = message.splitlines()
    for i, line in enumerate(lines):
        if _DECLARE in line:
            lines[i] = line.replace(_DECLARE, "", 1)
            if i + 1 < len(lines) and lines[i + 1].strip() == "^":
                lines[i + 1] = lines[i + 1][len(_DECLARE) :]
    return "\n".join(lines)


def _looks_like_query(sql: str) -> bool:
    words = sql.lstrip("(").split(None, 1)
    return bool(words) and words[0].lower() in {"select", "with", "values", "table"}


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
