"""
Running an LLM's query safely, with a context manager per database: limiting
it to one read-only query, timing it out, and reporting its errors.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, transaction
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
                    # A newline ends any trailing -- comment, before the rest
                    # of the statement psycopg sends.
                    cursor.execute(f"{sql}\n")
                    yield cursor
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
# Functions that reach outside the database.
_SQLITE_DENIED_FUNCTIONS = frozenset(
    {"load_extension", "fts3_tokenizer", "readfile", "writefile", "edit"}
)
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
    SQLite has no roles, so queries run on the alias's usual connection, in
    a savepoint that is rolled back, with an authorizer callback allowing
    only reads of the reader's columns, and a progress handler enforcing the
    timeout. Python's sqlite3 module runs only one statement per execute().
    """
    import sqlite3

    allowed = {
        (table.name, column) for table in reader.tables for column in table.columns
    }

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
            # Inside a view, whose own columns are checked at the top
            # level, or a trigger. Both are the developer's SQL.
            source is not None
            # No column, as for count(*), so no data.
            or arg2 == ""
            or (arg1, arg2) in allowed
            or arg1 in _SQLITE_TABLE_FUNCTIONS
        ):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2 not in _SQLITE_DENIED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    deadline = time.monotonic() + timeout

    def progress() -> int:
        # Non-zero interrupts the query.
        return int(time.monotonic() > deadline)

    connection.ensure_connection()
    sqlite_connection = connection.connection
    with transaction.atomic(using=connection.alias):
        try:
            with connection.chunked_cursor() as cursor:
                # Only around the query, not Django's own statements, such as
                # the savepoints of the atomic block.
                sqlite_connection.set_authorizer(authorize)
                sqlite_connection.set_progress_handler(progress, _SQLITE_PROGRESS_STEPS)
                try:
                    cursor.execute(sql)
                    yield cursor
                finally:
                    sqlite_connection.set_authorizer(None)
                    sqlite_connection.set_progress_handler(None, 0)
        except DatabaseError as exc:
            raise _sqlite_error(exc, sql, timeout) from None
        finally:
            connection.set_rollback(True)


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
