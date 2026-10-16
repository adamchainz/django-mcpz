"""
What each supported database does to run an LLM's query safely: limiting it
to one read-only query, timing it out, and reporting its errors.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.utils import CursorWrapper

from django_mcpz.server import ToolError

if TYPE_CHECKING:
    from django_mcpz.sql.readers import SQLReader

ONLY_SELECT = "Only a single SELECT statement is allowed, or WITH, VALUES, or TABLE."


def get_backend(connection: BaseDatabaseWrapper) -> Backend:
    backend = _BACKENDS.get(connection.vendor)
    if backend is None:
        raise ImproperlyConfigured(
            "The SQL tools support PostgreSQL and SQLite, not"
            f" {connection.display_name}, for the database alias"
            f" {connection.alias!r}."
        )
    return backend


class Backend:
    """How to run a query on one kind of database."""

    # The name of the SQL dialect, for the LLM.
    name: str
    # Whether the reader's alias logs in as its own role, granted access by
    # another alias, as opposed to the database enforcing access per query.
    uses_roles: bool

    def check(self, connection: BaseDatabaseWrapper) -> None:
        """Check the connection can run the tools, when they're added."""

    def time_note(self, connection: BaseDatabaseWrapper) -> str:
        """How the database treats dates and times, for the table list."""
        raise NotImplementedError  # pragma: no cover

    def execute(
        self,
        connection: BaseDatabaseWrapper,
        reader: SQLReader,
        sql: str,
        timeout: float,
    ) -> contextlib.AbstractContextManager[CursorWrapper]:
        """
        Run the query, inside the atomic block that rolls it back, yielding
        the cursor to fetch from, and converting errors in the query, raised
        then or while fetching, to ToolErrors.
        """
        raise NotImplementedError  # pragma: no cover

    def finish(self, connection: BaseDatabaseWrapper) -> None:
        """Clean up after the query's transaction ended."""


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


class PostgreSQLBackend(Backend):
    """
    Queries run as the reader's role, which may only read its columns, in a
    read-only transaction, through Django's server-side cursor. PostgreSQL
    wraps the query as DECLARE ... CURSOR FOR <sql>, sent with the extended
    query protocol, which accepts exactly one statement, which must be a
    query: no data-modifying statements, even in a WITH clause, and no
    transaction control.
    """

    name = "PostgreSQL"
    uses_roles = True

    def check(self, connection: BaseDatabaseWrapper) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError:  # pragma: no cover
            raise ImproperlyConfigured("The SQL tools need psycopg 3.") from None
        if connection.settings_dict["OPTIONS"].get("assume_role"):
            raise ImproperlyConfigured(
                f"add_sql_tools() needs {connection.alias!r} to log in as the"
                " reader role, not use assume_role, since queries could switch"
                " back to the login role with set_config('role', ...)."
            )

    def time_note(self, connection: BaseDatabaseWrapper) -> str:
        return (
            f"Queries run with the time zone {connection.timezone_name}, which"
            " applies to date functions on timestamp with time zone columns."
        )

    @contextlib.contextmanager
    def execute(
        self,
        connection: BaseDatabaseWrapper,
        reader: SQLReader,
        sql: str,
        timeout: float,
    ) -> Iterator[CursorWrapper]:
        from psycopg.types.json import JsonbLoader

        declare_length = 0
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(_SETUP_SQL, [f"{max(1, int(timeout * 1000))}ms"])
                privileged = cursor.fetchone()[1]
            if privileged:
                raise ImproperlyConfigured(
                    f"The database alias {connection.alias!r} must log in as a"
                    " role that is not a superuser, cannot bypass row-level"
                    " security, and is not a member of other roles, or"
                    " queries could read beyond the role's grants."
                )
            with connection.chunked_cursor() as cursor:
                declare_length = _declare_length(cursor.cursor)
                # Django loads jsonb as text, for JSONField. Load it as JSON
                # here, so it's nested JSON in results.
                cursor.cursor.adapters.register_loader("jsonb", JsonbLoader)
                # A newline ends any trailing -- comment, before the rest of
                # the statement psycopg sends.
                cursor.execute(f"{sql}\n")
                yield cursor
        except DatabaseError as exc:
            raise _postgresql_error(exc, sql, timeout, declare_length) from None

    def finish(self, connection: BaseDatabaseWrapper) -> None:
        """
        Release session-level advisory locks the query took, with functions
        like pg_advisory_lock(). Unlike settings, they outlast the rollback,
        and would be held by the connection for later requests.
        """
        import psycopg.pq

        pg_connection = connection.connection
        if (
            pg_connection is None
            or pg_connection.closed
            # In an outer transaction, which failed.
            or connection.needs_rollback
            or pg_connection.info.transaction_status
            == psycopg.pq.TransactionStatus.INERROR
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


class SQLiteBackend(Backend):
    """
    SQLite has no roles, so queries run on the alias's usual connection,
    with an authorizer callback allowing only reads of the reader's columns,
    and a progress handler enforcing the timeout. Python's sqlite3 module
    runs only one statement per execute().
    """

    name = "SQLite"
    uses_roles = False

    def time_note(self, connection: BaseDatabaseWrapper) -> str:
        return (
            "Dates and times are stored as ISO 8601 text, in UTC, so use"
            " SQLite's date and time functions on them."
        )

    @contextlib.contextmanager
    def execute(
        self,
        connection: BaseDatabaseWrapper,
        reader: SQLReader,
        sql: str,
        timeout: float,
    ) -> Iterator[CursorWrapper]:
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
            if (
                action == sqlite3.SQLITE_FUNCTION
                and arg2 not in _SQLITE_DENIED_FUNCTIONS
            ):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        deadline = time.monotonic() + timeout

        def progress() -> int:
            # Non-zero interrupts the query.
            return int(time.monotonic() > deadline)

        connection.ensure_connection()
        sqlite_connection = connection.connection
        try:
            with connection.chunked_cursor() as cursor:
                # Only around the query, not Django's own statements, such
                # as the savepoints of the atomic block.
                sqlite_connection.set_authorizer(authorize)
                sqlite_connection.set_progress_handler(
                    progress, _SQLITE_PROGRESS_STEPS
                )
                try:
                    cursor.execute(sql)
                    yield cursor
                finally:
                    sqlite_connection.set_authorizer(None)
                    sqlite_connection.set_progress_handler(None, 0)
        except DatabaseError as exc:
            raise _sqlite_error(exc, sql, timeout) from None


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


_BACKENDS: dict[str, Backend] = {
    "postgresql": PostgreSQLBackend(),
    "sqlite": SQLiteBackend(),
}
