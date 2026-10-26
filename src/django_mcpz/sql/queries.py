"""
Running an LLM's query safely, with a context manager per database: limiting
it to one read-only query, timing it out, and reporting its errors.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.utils import CursorWrapper

from django_mcpz.server import ToolError

if TYPE_CHECKING:
    from django_mcpz.sql.readers import SQLReader

ONLY_SELECT = "Only a single SELECT statement is allowed, or WITH, VALUES, or TABLE."

Query = Callable[
    [BaseDatabaseWrapper, "SQLReader", str, float],
    contextlib.AbstractContextManager[CursorWrapper],
]


def get_query(connection: BaseDatabaseWrapper) -> Query:
    """
    The context manager that runs a query on the connection's database, in
    a transaction, or savepoint, that it rolls back, yielding the cursor to
    fetch from, and converting errors in the query to ToolErrors.
    """
    try:
        return QUERIES[connection.vendor]
    except KeyError:
        raise ImproperlyConfigured(
            "The SQL tools support PostgreSQL and SQLite, not"
            f" {connection.display_name}, for the database alias"
            f" {connection.alias!r}."
        ) from None


def _timeout_error(timeout: float) -> ToolError:
    return ToolError(
        f"The query was cancelled after running for {timeout:g} seconds."
        " Make it cheaper, for example by filtering earlier or aggregating"
        " less data."
    )


def _looks_like_query(sql: str) -> bool:
    words = sql.lstrip("(").split(None, 1)
    return bool(words) and words[0].lower() in {"select", "with", "values", "table"}


# PostgreSQL

# SQLSTATE classes of errors in the query itself, which the LLM can fix.
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


@contextlib.contextmanager
def postgresql_query(
    connection: BaseDatabaseWrapper, reader: SQLReader, sql: str, timeout: float
) -> Iterator[CursorWrapper]:
    """
    Queries run as the reader's role, which may only read its columns, in a
    read-only transaction, through Django's server-side cursor. PostgreSQL
    wraps the query as DECLARE ... CURSOR FOR <sql>, sent with the extended
    query protocol, which accepts exactly one statement, which must be a
    query: no data-modifying statements, even in a WITH clause, and no
    transaction control.
    """
    from psycopg.types.json import JsonbLoader

    declare_length = 0
    try:
        with transaction.atomic(using=connection.alias):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(_SETUP_SQL, [f"{max(1, int(timeout * 1000))}ms"])
                    privileged = cursor.fetchone()[1]
                if privileged:
                    raise ImproperlyConfigured(
                        f"The database alias {connection.alias!r} must log in as"
                        " a role that is not a superuser, cannot bypass"
                        " row-level security, and is not a member of other"
                        " roles, or queries could read beyond the role's grants."
                    )
                with connection.chunked_cursor() as cursor:
                    declare_length = _declare_length(cursor.cursor)
                    # Django loads jsonb as text, for JSONField. Load it as
                    # JSON here, so it's nested JSON in results.
                    cursor.cursor.adapters.register_loader("jsonb", JsonbLoader)
                    # statement_timeout times each FETCH separately, and the
                    # query can change it with set_config(), so this cancels
                    # the whole query, rows and all, after the timeout.
                    watchdog = threading.Timer(
                        timeout, _cancel, [connection.connection]
                    )
                    watchdog.start()
                    try:
                        # A newline ends any trailing -- comment, before the
                        # rest of the statement psycopg sends.
                        cursor.execute(f"{sql}\n")
                        yield cursor
                    finally:
                        watchdog.cancel()
            except DatabaseError as exc:
                raise _postgresql_error(exc, sql, timeout, declare_length) from None
            finally:
                # Never commit: there is nothing to commit, and anything that
                # slips past the read-only transaction is undone, including
                # settings changed with set_config(). Requested last, as
                # Django refuses queries after it.
                connection.set_rollback(True)
    finally:
        _release_advisory_locks(connection)


def _cancel(pg_connection: Any) -> None:
    # cancel_safe() is new in psycopg 3.2.
    getattr(pg_connection, "cancel_safe", pg_connection.cancel)()


def _release_advisory_locks(connection: BaseDatabaseWrapper) -> None:
    """
    Release session-level advisory locks the query took, with functions like
    pg_advisory_lock(). Unlike settings, they outlast the rollback, and would
    be held by the connection for later requests.
    """
    import psycopg.pq

    pg_connection = connection.connection
    if (
        pg_connection is None
        or pg_connection.closed
        # In an outer transaction, which failed.
        or connection.needs_rollback
        or pg_connection.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
    ):
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock_all()")


def _declare_length(cursor: Any) -> int:
    """
    The length of the DECLARE that psycopg's server-side cursor puts before
    the query, so error positions can be moved to match the query alone.
    Django's cursors are NO SCROLL.
    """
    name = '"' + cursor.name.replace('"', '""') + '"'
    hold = " WITH HOLD" if cursor.withhold else ""
    return len(f"DECLARE {name} NO SCROLL CURSOR{hold} FOR ")


def _postgresql_error(
    exc: DatabaseError, sql: str, timeout: float, declare_length: int
) -> Exception:
    """
    The error to report for a query that failed: a ToolError for problems in
    the query, which the LLM can fix, or the error itself otherwise.
    """
    import psycopg

    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate == _QUERY_CANCELED:
        return _timeout_error(timeout)
    if sqlstate is None and isinstance(cause, psycopg.DataError):
        # A value psycopg can't load, such as an infinite or BC date.
        return ToolError(
            f"Query error: {cause}. Cast the column to text to read such values."
        )
    if sqlstate is None or sqlstate[:2] not in _QUERY_ERROR_CLASSES:
        return exc
    message = _postgresql_message(cause, sql, declare_length)
    if sqlstate == _SYNTAX_ERROR and (
        "multiple commands" in message or not _looks_like_query(sql)
    ):
        message += f"\n{ONLY_SELECT}"
    return ToolError(f"Query error: {message}")


def _postgresql_message(error: Any, sql: str, declare_length: int) -> str:
    """
    PostgreSQL's message, with the line of the query at the error position
    marked by a caret, as psql shows. Built from the error's fields, since
    PostgreSQL's own rendering quotes the whole DECLARE, truncated.
    """
    diag = error.diag
    message = diag.message_primary or str(error)
    position = int(diag.statement_position or 0) - 1 - declare_length
    if 0 <= position <= len(sql):
        start = sql.rfind("\n", 0, position) + 1
        end = sql.find("\n", position)
        line_number = sql.count("\n", 0, position) + 1
        prefix = f"LINE {line_number}: "
        line = sql[start : end if end != -1 else len(sql)]
        message += f"\n{prefix}{line}\n{' ' * (len(prefix) + position - start)}^"
    if diag.message_hint:
        message += f"\nHINT:  {diag.message_hint}"
    return message


# SQLite

# Table-valued functions that read no stored data, which the authorizer sees
# as reads of tables by their names.
_SQLITE_TABLE_FUNCTIONS = frozenset({"json_each", "json_tree", "generate_series"})
# Functions that reach outside the database, and those Django defines in
# Python, which can take unbounded memory or time, beyond the reach of
# SQLite's length limit and the progress handler.
_SQLITE_DENIED_FUNCTIONS = frozenset(
    {
        "load_extension",
        "fts3_tokenizer",
        "readfile",
        "writefile",
        "edit",
        "lpad",
        "rpad",
        "repeat",
        "regexp",
    }
)
# The longest string or blob a query may make, bounding the memory that one
# call to a function like zeroblob() or printf() can take.
_SQLITE_MAX_LENGTH = 16 * 1024 * 1024
# SQLite result codes for errors in the query itself, which the LLM can fix.
# Others, such as the database being locked or corrupt, are raised as such.
_SQLITE_QUERY_ERRORS = frozenset(
    {"SQLITE_ERROR", "SQLITE_AUTH", "SQLITE_RANGE", "SQLITE_MISMATCH", "SQLITE_TOOBIG"}
)
# How often the progress handler checks the timeout, in virtual machine
# instructions.
_SQLITE_PROGRESS_STEPS = 1000


@contextlib.contextmanager
def sqlite_query(
    connection: BaseDatabaseWrapper, reader: SQLReader, sql: str, timeout: float
) -> Iterator[CursorWrapper]:
    """
    SQLite has no roles, so queries run on a fresh connection to the alias's
    database, closed afterwards, set to query only, with an authorizer
    callback allowing only reads of the reader's columns, and a progress
    handler enforcing the timeout. A fresh connection, since sqlite3 can't
    read back an authorizer or progress handler the app may have set on its
    own, to restore it. Python's sqlite3 module runs only one statement per
    execute().
    """
    import sqlite3

    deadline = time.monotonic() + timeout

    def progress() -> int:
        # Non-zero interrupts the query.
        return int(time.monotonic() > deadline)

    if connection.settings_dict["NAME"] in ("", ":memory:"):
        raise ImproperlyConfigured(
            f"The SQL tools can't query the database alias {connection.alias!r},"
            " a private in-memory SQLite database, which only its own"
            " connection can open."
        )
    fresh = connections.create_connection(connection.alias)
    # Hooks registered on the alias's connection see the query too.
    fresh.execute_wrappers = list(connection.execute_wrappers)
    try:
        with fresh.cursor() as cursor:
            # A second layer under the authorizer, for this connection only.
            cursor.execute("PRAGMA query_only = ON")
        try:
            _sqlite_check(fresh, reader, sql)
        except DatabaseError as exc:
            raise _sqlite_error(exc, sql, timeout) from None
        fresh.connection.set_authorizer(_sqlite_authorizer(reader, checked=True))
        fresh.connection.set_progress_handler(progress, _SQLITE_PROGRESS_STEPS)
        fresh.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _SQLITE_MAX_LENGTH)
        with fresh.chunked_cursor() as cursor:
            try:
                cursor.execute(sql)
                yield cursor
            except DatabaseError as exc:
                raise _sqlite_error(exc, sql, timeout) from None
    finally:
        fresh.close()


def _sqlite_check(fresh: BaseDatabaseWrapper, reader: SQLReader, sql: str) -> None:
    """
    Check the query's reads against a copy of the database's schema, in
    memory, with every view made a table of its columns, compiling the query
    with EXPLAIN, not running it.

    In the real database, the authorizer sees reads inside views as reads of
    the tables behind them, tagged with the view's name, as it should allow.
    But it sees reads inside a WITH clause's common table expressions the
    same way, tagged with their names, which the query picks, so can match a
    view's. With no views, no read needs allowing for its tag, so a query
    that passes reads only the reader's columns, as the views and tables it
    names are the same in the real database.
    """
    import sqlite3

    from django_mcpz.sql.readers import quote_name

    real = fresh.connection
    objects = real.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite^_%' ESCAPE '^'"
    ).fetchall()
    shadow = type(fresh)({**fresh.settings_dict, "NAME": ":memory:"}, fresh.alias)
    try:
        shadow.ensure_connection()
        for kind, name, create in objects:
            if kind == "table" and not create.upper().startswith("CREATE VIRTUAL"):
                try:
                    shadow.connection.execute(create)
                    continue
                except sqlite3.Error:
                    pass
            quoted = quote_name(name)
            columns = [
                quote_name(column)
                for _, column, *_ in real.execute(f"PRAGMA table_xinfo({quoted})")
            ]
            shadow.connection.execute(f"CREATE TABLE {quoted} ({', '.join(columns)})")
        shadow.connection.set_authorizer(
            _sqlite_authorizer(
                reader,
                checked=False,
                schema=frozenset(name for _, name, _ in objects),
            )
        )
        with shadow.cursor() as cursor:
            cursor.execute(f"EXPLAIN {sql}")
    finally:
        shadow.close()


def _sqlite_authorizer(
    reader: SQLReader, *, checked: bool, schema: frozenset[str] = frozenset()
) -> Callable[..., int]:
    """
    An authorizer allowing only reads of the reader's columns, where schema
    holds the names of the database's tables and views. Once the query is
    checked, with _sqlite_check(), it also allows reads inside views and
    common table expressions, which the check covered.
    """
    import sqlite3

    allowed = {
        (table.name, column) for table in reader.tables for column in table.columns
    }
    tables = {table.name for table in reader.tables}

    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and (
            (checked and source is not None)
            or (arg1, arg2) in allowed
            # No column, as for count(*), so no data, but only of the
            # reader's tables, or of common table expressions, not tables in
            # the schema. Once checked, also of tables behind views, which
            # SQLite reads this way for count(*) on a view.
            or (arg2 == "" and (checked or arg1 in tables or arg1 not in schema))
            or arg1 in _SQLITE_TABLE_FUNCTIONS
        ):
            return sqlite3.SQLITE_OK
        if (
            action == sqlite3.SQLITE_FUNCTION
            and arg2 is not None
            and arg2.lower() not in _SQLITE_DENIED_FUNCTIONS
        ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def _sqlite_error(exc: DatabaseError, sql: str, timeout: float) -> Exception:
    """
    The error to report for a query that failed: a ToolError for problems in
    the query, which the LLM can fix, or the error itself otherwise.
    """
    cause = exc.__cause__
    name = getattr(cause, "sqlite_errorname", None)
    message = str(cause)
    if name == "SQLITE_INTERRUPT":
        return _timeout_error(timeout)
    if name is None and "one statement at a time" in message:
        # From the sqlite3 module, not SQLite.
        return ToolError(f"Query error: {message}\n{ONLY_SELECT}")
    if name not in _SQLITE_QUERY_ERRORS:
        return exc
    if message == "not authorized":
        message += f"\n{ONLY_SELECT}"
    return ToolError(f"Query error: {message}")


QUERIES: dict[str, Query] = {
    "postgresql": postgresql_query,
    "sqlite": sqlite_query,
}
