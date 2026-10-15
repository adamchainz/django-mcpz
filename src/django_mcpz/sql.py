"""
Tools that let a language model run read-only SQL queries against PostgreSQL,
limited to the tables, typically views, of a list of models.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any

import msgspec
import msgspec.json
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Field, Model
from django.http import HttpRequest
from django_msgspec import enc_hook as django_enc_hook

from django_mcpz.server import MCPServer, ToolError

try:
    import psycopg
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

_ONLY_SELECT = (
    "Only a single SELECT statement is allowed, or WITH, VALUES, or TABLE."
)


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
    models: Sequence[type[Model]],
    using: str,
    context: str | None = None,
    max_rows: int = 100,
    max_size: int = 50_000,
    timeout: float = 5.0,
    name_prefix: str = "sql",
    permission: Callable[[HttpRequest], bool] | str | None = None,
) -> None:
    """
    Add two tools to the server: one describing the tables of the given
    models, as SQL, and one running a read-only query against them.

    The queries run on the database connection named by using, which should
    log in as a PostgreSQL role that may only SELECT from those tables. That
    role is the security boundary. This adds read-only transactions, a single
    statement per call, a statement timeout, and limits on the result.
    """
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise ImproperlyConfigured(
            f"add_sql_tools() needs a PostgreSQL database, but {using!r} is"
            f" {connection.vendor}."
        )
    if psycopg is None:  # pragma: no cover
        raise ImproperlyConfigured("add_sql_tools() needs psycopg 3.")
    if not models:
        raise ImproperlyConfigured("add_sql_tools() needs at least one model.")
    if max_rows < 1:
        raise ImproperlyConfigured("max_rows must be at least 1.")
    if max_size < 1000:
        raise ImproperlyConfigured("max_size must be at least 1000.")
    if timeout <= 0:
        raise ImproperlyConfigured("timeout must be positive.")

    model_list = list(models)
    tables = ", ".join(model._meta.db_table for model in model_list)
    schema_name = f"{name_prefix}_schema"
    query_name = f"{name_prefix}_query"

    @server.tool(
        name=schema_name,
        description=(
            f"The tables {query_name} can query, as CREATE TABLE statements"
            " with comments explaining the data. Call this before writing"
            f" queries. Tables: {tables}."
        ),
        read_only=True,
        idempotent=True,
        open_world=False,
        permission=permission,
    )
    def sql_schema(request: HttpRequest) -> str:
        return describe_models(model_list, using=using, context=context)

    @server.tool(
        name=query_name,
        description=(
            "Run one read-only PostgreSQL SELECT query and get the result rows,"
            ' as JSON: "columns" names the columns, and "rows" holds one array'
            f" of values per row. Only these tables are available: {tables}."
            f" Call {schema_name} for their columns first. At most {max_rows}"
            " rows are returned, so aggregate, filter, and sort in SQL rather"
            " than fetching everything. Queries time out after"
            f" {timeout:g} seconds."
        ),
        read_only=True,
        idempotent=True,
        open_world=False,
        permission=permission,
    )
    def sql_query(request: HttpRequest, params: SQLQueryParams) -> msgspec.Raw:
        columns, rows, more = run_query(
            params.sql, using=using, max_rows=max_rows, timeout=timeout
        )
        return encode_result(columns, rows, more=more, max_size=max_size)


def run_query(
    sql: str, *, using: str, max_rows: int, timeout: float
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """
    Run a query in a read-only transaction, returning the column names, up to
    max_rows rows, and whether more rows were left unfetched.

    The query runs through a server-side cursor, so PostgreSQL wraps it as
    DECLARE ... CURSOR FOR <sql>, sent with the extended query protocol. That
    accepts exactly one statement, which must be a query: no data-modifying
    statements, even in a WITH clause, and no transaction control. Rows are
    fetched only up to the limit, so a large result is never transferred.
    """
    connection = connections[using]
    sql = sql.strip().rstrip(";").rstrip()
    if not sql:
        raise ToolError("The SQL is empty.")
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
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{int(timeout * 1000)}ms"],
                )
            with psycopg.ServerCursor(pg_connection, CURSOR_NAME) as cursor:
                # A newline ends any trailing -- comment, before the rest of
                # the statement psycopg sends.
                cursor.execute(f"{sql}\n")
                assert cursor.description is not None
                columns = [column.name for column in cursor.description]
                rows = cursor.fetchmany(max_rows + 1)
    except psycopg.Error as exc:
        raise _query_error(exc, sql, timeout) from None
    finally:
        _release_advisory_locks(connection)
    return columns, rows[:max_rows], len(rows) > max_rows


def _release_advisory_locks(connection: BaseDatabaseWrapper) -> None:
    """
    Release session-level advisory locks the query took, with functions like
    pg_advisory_lock(). Unlike everything else a query can do in a read-only
    transaction, they outlast the rollback, and would be held by the
    connection for later requests.
    """
    pg_connection = connection.connection
    if pg_connection is None or pg_connection.closed:
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
    context: strings longer than a tenth of that are shortened, and rows that
    do not fit are dropped, with a note saying so.
    """
    max_value = max_size // 10
    budget = max_size - len(_encoder.encode(columns)) - 200
    kept: list[list[Any]] = []
    shortened = False
    for row in rows:
        values = []
        for value in row:
            if isinstance(value, str) and len(value) > max_value:
                value = (
                    f"{value[:max_value]}… [{len(value) - max_value} more"
                    " characters]"
                )
                shortened = True
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
            "Long text values were shortened. Select substr() of them to read"
            " specific parts."
        )
    if notes:
        result["truncated"] = True
        result["note"] = " ".join(notes)
    # Encoded here, rather than by the server, for decimals as numbers.
    return msgspec.Raw(_encoder.encode(result))


# Schema description


def describe_models(
    model_list: Sequence[type[Model]],
    *,
    using: str,
    context: str | None = None,
) -> str:
    """
    Describe the tables of the models as CREATE TABLE statements, with
    comments from each model's db_table_comment or docstring, and each
    field's db_comment or help_text, verbose name, and choices.
    """
    connection = connections[using]
    tables = {model._meta.db_table for model in model_list}
    parts = []
    if context:
        parts.append(_comment(context))
    parts.append(
        _comment(
            f"Queries run with the time zone {connection.timezone_name}, which"
            " applies to date functions on timestamp with time zone columns."
        )
    )
    for model in model_list:
        parts.append(_describe_model(model, connection, tables))
    return "\n\n".join(parts) + "\n"


def _describe_model(
    model: type[Model], connection: BaseDatabaseWrapper, tables: set[str]
) -> str:
    opts = model._meta
    lines = []
    description = opts.db_table_comment or _docstring(model)
    if description:
        lines.append(_comment(description))
    lines.append(f"CREATE TABLE {opts.db_table} (")
    columns = []
    for field in opts.concrete_fields:
        column = f"  {field.column} {_db_type(field, connection)}"
        if not field.null:
            column += " NOT NULL"
        if field.primary_key:
            column += " PRIMARY KEY"
        if field.is_relation and field.related_model._meta.db_table in tables:  # type: ignore[union-attr]
            target = field.target_field  # type: ignore[attr-defined]
            column += f" REFERENCES {target.model._meta.db_table} ({target.column})"
        columns.append((column, _field_notes(field)))
    for i, (column, notes) in enumerate(columns):
        if i < len(columns) - 1:
            column += ","
        if notes:
            column += f"  -- {notes}"
        lines.append(column)
    lines.append(");")
    return "\n".join(lines)


def _db_type(field: Field[Any, Any], connection: BaseDatabaseWrapper) -> str:
    if field.is_relation:
        return str(field.target_field.rel_db_type(connection))  # type: ignore[attr-defined]
    return str(field.db_type(connection))


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
    if verbose_name.lower() != field.name.replace("_", " ").lower():
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
