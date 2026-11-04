"""
Running an LLM's query safely, with a context manager per database: limiting
it to one read-only query, timing it out, and reporting its errors.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.utils import CursorWrapper

from django_mcpz.server import ToolError

Query = Callable[
    [BaseDatabaseWrapper, str, float],
    contextlib.AbstractContextManager[CursorWrapper],
]


def get_query(connection: BaseDatabaseWrapper) -> Query:
    """
    The context manager that runs a query on the connection's database, in
    a transaction, or savepoint, that it rolls back, yielding the cursor to
    fetch from, and converting errors in the query to ToolErrors.

    These limit what a query can do beyond reading, such as writing or
    running forever, but not what it reads, which the query checker does,
    before the query gets here.
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


@contextlib.contextmanager
def postgresql_query(
    connection: BaseDatabaseWrapper, sql: str, timeout: float
) -> Iterator[CursorWrapper]:
    """
    Queries run on a fresh connection to the alias's database, closed
    afterwards, in a read-only transaction that is never committed, through
    Django's server-side cursor, with a statement timeout and a watchdog. A
    fresh connection, so the transaction is the query's own, rather than
    part of the request's, which may have run queries already, and so the
    watchdog can't cancel the app's own statements.

    PostgreSQL wraps the query as DECLARE ... CURSOR FOR <sql>, sent with the
    extended query protocol, which accepts exactly one statement, which must
    be a query: no data-modifying statements, even in a WITH clause, and no
    transaction control.
    """
    from psycopg.types.json import JsonbLoader

    declare_length = 0
    fresh = connections.create_connection(connection.alias)
    # Hooks registered on the alias's connection see the query too.
    fresh.execute_wrappers = list(connection.execute_wrappers)
    try:
        fresh.set_autocommit(False)
        try:
            with fresh.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{max(1, int(timeout * 1000))}ms"],
                )
            with fresh.chunked_cursor() as cursor:
                declare_length = _declare_length(cursor.cursor)
                # Django loads jsonb as text, for JSONField. Load it as JSON
                # here, so it's nested JSON in results.
                cursor.cursor.adapters.register_loader("jsonb", JsonbLoader)
                # statement_timeout times each FETCH separately, so this
                # cancels the whole query, rows and all, after the timeout.
                with _watchdog(fresh.connection, timeout):
                    # A newline ends any trailing -- comment, before the rest
                    # of the statement psycopg sends.
                    cursor.execute(f"{sql}\n")
                    yield cursor
        except DatabaseError as exc:
            raise _postgresql_error(exc, sql, timeout, declare_length) from None
    finally:
        # Roll back before closing, which, with Django's connection pool,
        # returns the connection to the pool, which would otherwise roll it
        # back itself, with a warning. Not if the connection broke, when
        # closing is enough.
        if fresh.connection is not None and not fresh.connection.closed:
            with contextlib.suppress(DatabaseError):
                fresh.rollback()
        fresh.close()


@contextlib.contextmanager
def _watchdog(pg_connection: Any, timeout: float) -> Iterator[None]:
    """
    Cancel whatever the connection is running once the timeout has passed,
    unless the block has finished by then. The lock stops a cancel going out
    after it finishes, which could cancel the next statement, such as the
    rollback. One sent before then reaches the query, or the session idle
    once the query is done, where PostgreSQL ignores it.
    """
    lock = threading.Lock()
    finished = False

    def cancel() -> None:
        with lock:
            if not finished:
                # cancel_safe() is new in psycopg 3.2.
                if hasattr(pg_connection, "cancel_safe"):
                    pg_connection.cancel_safe()
                else:
                    pg_connection.cancel()

    timer = threading.Timer(timeout, cancel)
    timer.start()
    try:
        yield
    finally:
        with lock:
            finished = True
        timer.cancel()


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

# The longest string or blob a query may make, bounding the memory that one
# call to a function like zeroblob() or printf() can take.
_SQLITE_MAX_LENGTH = 16 * 1024 * 1024
# SQLite result codes for errors in the query itself, which the LLM can fix.
# Others, such as the database being locked or corrupt, are raised as such.
_SQLITE_QUERY_ERRORS = frozenset(
    {
        "SQLITE_ERROR",
        "SQLITE_RANGE",
        "SQLITE_MISMATCH",
        "SQLITE_READONLY",
        "SQLITE_TOOBIG",
    }
)
# How often the progress handler checks the timeout, in virtual machine
# instructions.
_SQLITE_PROGRESS_STEPS = 1000


@contextlib.contextmanager
def sqlite_query(
    connection: BaseDatabaseWrapper, sql: str, timeout: float
) -> Iterator[CursorWrapper]:
    """
    Queries run on a fresh connection to the alias's database, closed
    afterwards, set to query only, with a progress handler enforcing the
    timeout, and a length limit on strings and blobs. A fresh connection,
    since sqlite3 can't read back a progress handler the app may have set on
    its own, to restore it. Python's sqlite3 module runs only one statement
    per execute().
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
            # For this connection only.
            cursor.execute("PRAGMA query_only = ON")
        fresh.connection.set_progress_handler(progress, _SQLITE_PROGRESS_STEPS)
        fresh.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _SQLITE_MAX_LENGTH)
        with fresh.chunked_cursor() as cursor:
            try:
                cursor.execute(sql)
                yield cursor
            except DatabaseError as exc:
                raise _sqlite_error(exc, sql, timeout) from None
    finally:
        _sqlite_close(fresh)


def _sqlite_close(connection: BaseDatabaseWrapper) -> None:
    # Django's close() leaves in-memory databases open, to keep their data,
    # such as in tests, but these connections are done with.
    if connection.connection is not None:
        connection.connection.close()


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
    if name not in _SQLITE_QUERY_ERRORS:
        return exc
    return ToolError(f"Query error: {message}")


# The functions queries may call, by database, as checked before running
# them: aggregates, window functions, and functions on numbers, text, dates,
# and JSON, none of which reach beyond the query's own data, such as into
# settings, files, or other sessions. Mostly in both databases, so the same
# queries work on each.
_COMMON_FUNCTIONS = frozenset(
    {
        # Aggregates.
        "avg",
        "count",
        "max",
        "min",
        "sum",
        # Window functions.
        "dense_rank",
        "first_value",
        "lag",
        "last_value",
        "lead",
        "ntile",
        "rank",
        "row_number",
        # Numbers.
        "abs",
        "ceil",
        "floor",
        "round",
        # Nulls.
        "coalesce",
        "nullif",
        # Text.
        "length",
        "lower",
        "ltrim",
        "replace",
        "rtrim",
        "substr",
        "substring",
        "trim",
        "upper",
        # Dates and times.
        "current_date",
        "current_timestamp",
    }
)
FUNCTIONS: dict[str, frozenset[str]] = {
    "postgresql": _COMMON_FUNCTIONS
    | {
        "date_part",
        "date_trunc",
        "extract",
        "greatest",
        "json_array_length",
        "jsonb_array_length",
        "least",
        "position",
        "string_agg",
        "to_char",
    },
    "sqlite": _COMMON_FUNCTIONS
    | {
        "date",
        "datetime",
        "group_concat",
        "instr",
        "json_array_length",
        "json_extract",
        "julianday",
        "strftime",
        "time",
        "total",
    },
}


QUERIES: dict[str, Query] = {
    "postgresql": postgresql_query,
    "sqlite": sqlite_query,
}
