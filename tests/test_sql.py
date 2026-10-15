from __future__ import annotations

import datetime as dt
import ipaddress
import uuid
from decimal import Decimal
from typing import Any
from unittest import mock

import msgspec.json
import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from django_mcpz.server import MCPServer, ToolError, public
from django_mcpz.sql import (
    _release_advisory_locks,
    _strip_declare,
    add_sql_tools,
    describe_models,
    encode_result,
    run_query,
)
from tests.models import Part, Undocumented, WidgetView
from tests.test_server import ServerTestCase, make_message

SCHEMA = """\
-- Test widgets.

-- Queries run with the time zone UTC, which applies to date functions on timestamp with time zone columns.

-- Widgets for sale, without their stock levels.
CREATE TABLE tests_widget_view (
  id bigint NOT NULL PRIMARY KEY,
  name varchar(100) NOT NULL,
  price numeric(6, 2) NOT NULL  -- In euros, with VAT.
);
"""


class AddSQLToolsTests(SimpleTestCase):
    def test_not_postgresql(self):
        server = MCPServer(name="s", version="1", auth=public)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            add_sql_tools(server, models=[WidgetView], using="default")
        assert str(excinfo.value) == (
            "add_sql_tools() needs a PostgreSQL database, but 'default' is sqlite."
        )

    def test_no_models(self):
        server = MCPServer(name="s", version="1", auth=public)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            add_sql_tools(server, models=[], using="reader")
        assert str(excinfo.value) == "add_sql_tools() needs at least one model."

    def test_max_rows(self):
        server = MCPServer(name="s", version="1", auth=public)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            add_sql_tools(server, models=[WidgetView], using="reader", max_rows=0)
        assert str(excinfo.value) == "max_rows must be at least 1."

    def test_max_size(self):
        server = MCPServer(name="s", version="1", auth=public)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            add_sql_tools(server, models=[WidgetView], using="reader", max_size=999)
        assert str(excinfo.value) == "max_size must be at least 1000."

    def test_timeout(self):
        server = MCPServer(name="s", version="1", auth=public)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            add_sql_tools(server, models=[WidgetView], using="reader", timeout=0)
        assert str(excinfo.value) == "timeout must be positive."

    def test_name_prefix_and_permission(self):
        server = MCPServer(name="s", version="1", auth=public)
        add_sql_tools(
            server,
            models=[WidgetView],
            using="reader",
            name_prefix="widgets",
            permission=lambda request: False,
        )
        assert list(server._tools) == ["widgets_schema", "widgets_query"]
        assert not server._tools["widgets_query"].permitted(mock.Mock())


class DescribeModelsTests(SimpleTestCase):
    def test_view(self):
        assert (
            describe_models([WidgetView], using="reader", context="Test widgets.")
            == SCHEMA
        )

    def test_fields(self):
        # Part references WidgetView, which is described, and Widget, which is
        # not, so only the first gets a REFERENCES.
        assert describe_models([WidgetView, Part, Undocumented], using="reader") == (
            "-- Queries run with the time zone UTC, which applies to date"
            " functions on timestamp with time zone columns.\n"
            "\n"
            "-- Widgets for sale, without their stock levels.\n"
            "CREATE TABLE tests_widget_view (\n"
            "  id bigint NOT NULL PRIMARY KEY,\n"
            "  name varchar(100) NOT NULL,\n"
            "  price numeric(6, 2) NOT NULL  -- In euros, with VAT.\n"
            ");\n"
            "\n"
            "-- Parts that make up\n"
            "-- widgets.\n"
            "CREATE TABLE tests_part (\n"
            "  id bigint NOT NULL PRIMARY KEY,\n"
            "  widget_id bigint NOT NULL REFERENCES tests_widget_view (id),\n"
            "  sku varchar(20) NOT NULL,  -- Stock keeping unit. Unique per"
            " supplier.\n"
            "  size varchar(1),  -- Values: 'S' = Small, 'L' = Large.\n"
            "  supplier_id bigint NOT NULL,\n"
            "  rank integer NOT NULL  -- Values: 1 = First, 2 = Second.\n"
            ");\n"
            "\n"
            "CREATE TABLE tests_undocumented (\n"
            "  id bigint NOT NULL PRIMARY KEY\n"
            ");\n"
        )


def decode(raw: msgspec.Raw) -> Any:
    return msgspec.json.decode(bytes(raw))


class EncodeResultTests(SimpleTestCase):
    def test_types(self):
        row = (
            None,
            True,
            3,
            Decimal("1.50"),
            dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC),
            dt.date(2026, 1, 2),
            dt.timedelta(days=1),
            uuid.UUID(int=1),
            {"a": [1]},
            ipaddress.ip_address("127.0.0.1"),
        )
        result = decode(
            encode_result(
                [f"c{i}" for i in range(len(row))], [row], more=False, max_size=1000
            )
        )
        assert result == {
            "columns": [f"c{i}" for i in range(len(row))],
            "rows": [
                [
                    None,
                    True,
                    3,
                    1.5,
                    "2026-01-02T03:04:00Z",
                    "2026-01-02",
                    "P1D",
                    "00000000-0000-0000-0000-000000000001",
                    {"a": [1]},
                    "127.0.0.1",
                ]
            ],
        }

    def test_more(self):
        result = decode(encode_result(["a"], [(1,)], more=True, max_size=1000))
        assert result == {
            "columns": ["a"],
            "rows": [[1]],
            "truncated": True,
            "note": (
                "Only the first 1 rows are shown. Aggregate, filter, or add a"
                " LIMIT to see the rest."
            ),
        }

    def test_size(self):
        rows = [(i, "x" * 90) for i in range(20)]
        result = decode(encode_result(["a", "b"], rows, more=False, max_size=1000))
        assert len(result["rows"]) == 8
        assert result["truncated"] is True
        assert result["note"].startswith("Only the first 8 rows are shown.")

    def test_long_value(self):
        result = decode(
            encode_result(
                ["a"], [("x" * 2000,), ("y",)], more=False, max_size=1000
            )
        )
        assert result == {
            "columns": ["a"],
            "rows": [["x" * 100 + "… [1900 more characters]"], ["y"]],
            "truncated": True,
            "note": (
                "Long text values were shortened. Select substr() of them to"
                " read specific parts."
            ),
        }

    def test_first_row_kept(self):
        # Even when one row is over budget, it is shown, rather than none.
        row = tuple("x" * 100 for _ in range(20))
        result = decode(encode_result(["a"] * 20, [row, row], more=False, max_size=1000))
        assert len(result["rows"]) == 1
        assert result["note"].startswith("Only the first 1 rows are shown.")


class StripDeclareTests(SimpleTestCase):
    def test_no_caret(self):
        assert _strip_declare('x\nLINE 1: DECLARE "mcp_query" CURSOR FOR y\nz') == (
            "x\nLINE 1: y\nz"
        )


class ReleaseAdvisoryLocksTests(SimpleTestCase):
    def test_not_connected(self):
        _release_advisory_locks(mock.Mock(connection=None))

    def test_closed(self):
        _release_advisory_locks(mock.Mock(connection=mock.Mock(closed=True)))


class SQLServerTests(ServerTestCase, TestCase):
    url = "/sql-mcp"
    databases = {"postgres", "reader"}

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        response = self.post(
            make_message("tools/call", {"name": name, "arguments": arguments or {}})
        )
        assert response.status_code == 200
        return response.json()["result"]

    def query(self, sql: str) -> Any:
        result = self.call("sql_query", {"sql": sql})
        if result["isError"]:
            return result["content"][0]["text"]
        return result["structuredContent"]

    def test_list(self):
        response = self.post(make_message("tools/list"))
        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert tools["sql_schema"]["description"] == (
            "The tables sql_query can query, as CREATE TABLE statements with"
            " comments explaining the data. Call this before writing queries."
            " Tables: tests_widget_view."
        )
        assert tools["sql_query"]["description"] == (
            "Run one read-only PostgreSQL SELECT query and get the result rows,"
            ' as JSON: "columns" names the columns, and "rows" holds one array'
            " of values per row. Only these tables are available:"
            " tests_widget_view. Call sql_schema for their columns first. At"
            " most 2 rows are returned, so aggregate, filter, and sort in SQL"
            " rather than fetching everything. Queries time out after 1 seconds."
        )
        assert tools["sql_query"]["annotations"] == {
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert tools["sql_query"]["inputSchema"]["required"] == ["sql"]

    def test_schema(self):
        result = self.call("sql_schema")
        assert result["content"] == [{"type": "text", "text": SCHEMA}]

    def test_query(self):
        assert self.query("SELECT count(*) FROM tests_widget_view") == {
            "columns": ["count"],
            "rows": [[3]],
        }

    def test_query_more(self):
        assert self.query(
            "SELECT name, price FROM tests_widget_view ORDER BY price;\n"
        ) == {
            "columns": ["name", "price"],
            "rows": [["Doohickey", 0.99], ["Sprocket", 4.5]],
            "truncated": True,
            "note": (
                "Only the first 2 rows are shown. Aggregate, filter, or add a"
                " LIMIT to see the rest."
            ),
        }

    def test_query_trailing_comment(self):
        assert self.query("SELECT 1 AS one -- the loneliest number") == {
            "columns": ["one"],
            "rows": [[1]],
        }

    def test_query_percent(self):
        assert self.query("SELECT '%s' LIKE '%%s' AS percent") == {
            "columns": ["percent"],
            "rows": [[True]],
        }

    def test_empty(self):
        assert self.query(" ; ") == "The SQL is empty."

    def test_permission_denied(self):
        # The role may read the view, but not the table behind it.
        assert self.query("SELECT * FROM tests_widget") == (
            "Query error: permission denied for table tests_widget"
        )

    def test_not_a_query(self):
        assert self.query("DELETE FROM tests_widget") == (
            'Query error: syntax error at or near "DELETE"\n'
            "LINE 1: DELETE FROM tests_widget\n"
            "        ^\n"
            "Only a single SELECT statement is allowed, or WITH, VALUES, or"
            " TABLE."
        )

    def test_syntax_error(self):
        assert self.query("SELECT FROM WHERE") == (
            'Query error: syntax error at or near "WHERE"\n'
            "LINE 1: SELECT FROM WHERE\n"
            "                    ^"
        )

    def test_multiple_statements(self):
        assert self.query("SELECT 1; DELETE FROM tests_widget") == (
            "Query error: cannot insert multiple commands into a prepared"
            " statement\n"
            "Only a single SELECT statement is allowed, or WITH, VALUES, or"
            " TABLE."
        )

    def test_escape_subquery(self):
        assert self.query("SELECT 1) q; COMMIT; --") == (
            'Query error: syntax error at or near ")"\n'
            "LINE 1: SELECT 1) q; COMMIT; --\n"
            "                ^"
        )

    def test_writing_with(self):
        assert self.query(
            "WITH d AS (DELETE FROM tests_widget RETURNING id) SELECT * FROM d"
        ) == (
            "Query error: DECLARE CURSOR must not contain data-modifying"
            " statements in WITH"
        )

    def test_read_write(self):
        # Under TestCase, the query runs in a savepoint, so PostgreSQL gives
        # a different reason than at the top level.
        assert self.query(
            "SELECT set_config('transaction_read_only', 'off', true)"
        ) == (
            "Query error: cannot set transaction read-write mode inside a"
            " read-only transaction"
        )

    def test_change_role(self):
        assert self.query("SELECT set_config('role', 'postgres', true)") == (
            'Query error: permission denied to set role "postgres"'
        )

    def test_timeout(self):
        assert self.query("SELECT pg_sleep(5)") == (
            "The query was cancelled after running for 1 seconds. Make it"
            " cheaper, for example by filtering earlier or aggregating less"
            " data."
        )

    def test_settings_reverted(self):
        self.query("SELECT set_config('statement_timeout', '0', false)")
        assert self.query("SELECT current_setting('statement_timeout') AS t") == {
            "columns": ["t"],
            "rows": [["1s"]],
        }

    def test_advisory_locks_released(self):
        self.query("SELECT pg_advisory_lock(1)")
        assert self.query(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
        ) == {"columns": ["count"], "rows": [[0]]}

    def test_server_error(self):
        # Errors that are not the query's fault are raised, for the server to
        # log, rather than shown to the model.
        with (
            mock.patch.object(
                psycopg.ServerCursor,
                "execute",
                side_effect=psycopg.OperationalError("connection lost"),
            ),
            self.assertLogs("django_mcpz", "ERROR"),
        ):
            assert self.query("SELECT 1") == "Tool 'sql_query' failed unexpectedly."


class RunQueryTransactionTests(TransactionTestCase):
    # The reader alias is not in a transaction here, unlike in TestCase.
    databases = {"postgres", "reader"}
    available_apps: list[str] = []

    def test_read_write(self):
        with pytest.raises(ToolError) as excinfo:
            run_query(
                "SELECT set_config('transaction_read_only', 'off', true)",
                using="reader",
                max_rows=10,
                timeout=1,
            )
        assert str(excinfo.value) == (
            "Query error: transaction read-write mode must be set before any query"
        )

    def test_autocommit_restored(self):
        assert run_query("SELECT 1", using="reader", max_rows=10, timeout=1) == (
            ["?column?"],
            [(1,)],
            False,
        )
        connection = connections["reader"]
        assert connection.get_autocommit()
        assert not connection.in_atomic_block
