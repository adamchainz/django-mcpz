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
    _fetch,
    _release_advisory_locks,
    _strip_declare,
    add_sql_tools,
    describe_models,
    encode_result,
    list_tables,
    run_query,
    sync_sql_grants,
)
from tests.models import (
    Child,
    Pair,
    Part,
    Reserved,
    Undocumented,
    WidgetName,
    WidgetPrice,
    WidgetView,
)
from tests.test_server import ServerTestCase, make_message

TABLES = """\
Test widgets.

Queries run with the time zone UTC, which applies to date functions on timestamp with time zone columns.

Tables:
- tests_widget_view: Widgets for sale, without their stock levels.

Call sql_schema with tables to see their columns.
"""

SCHEMA = """\
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

    def test_assume_role(self):
        server = MCPServer(name="s", version="1", auth=public)
        with (
            mock.patch.dict(
                connections["reader"].settings_dict["OPTIONS"], {"assume_role": "x"}
            ),
            pytest.raises(ImproperlyConfigured) as excinfo,
        ):
            add_sql_tools(server, models=[WidgetView], using="reader")
        assert str(excinfo.value).startswith(
            "add_sql_tools() needs 'reader' to log in as the reader role,"
        )

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


class ListTablesTests(SimpleTestCase):
    def test_tables(self):
        assert list_tables(
            [WidgetView, Undocumented, Reserved, Part],
            using="reader",
            schema_tool="widgets_schema",
        ) == (
            "Queries run with the time zone UTC, which applies to date"
            " functions on timestamp with time zone columns.\n"
            "\n"
            "Tables:\n"
            "- tests_widget_view: Widgets for sale, without their stock levels.\n"
            "- tests_undocumented\n"
            '- "order"\n'
            "- tests_part: Parts that make up widgets.\n"
            "\n"
            "Call widgets_schema with tables to see their columns.\n"
        )


class DescribeModelsTests(SimpleTestCase):
    def test_view(self):
        assert describe_models([WidgetView], using="reader") == SCHEMA

    def test_all_models(self):
        # References to tables outside the described ones, but queryable.
        assert describe_models(
            [Part], using="reader", all_models=[WidgetView, Part]
        ).splitlines()[4] == (
            "  widget_id bigint NOT NULL REFERENCES tests_widget_view (id),"
        )

    def test_fields(self):
        # Part references WidgetView, which is described, and Widget, which is
        # not, so only the first gets a REFERENCES.
        assert describe_models([WidgetView, Part, Undocumented], using="reader") == (
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


class DescribeEdgeCasesTests(SimpleTestCase):
    def test_quoting_and_no_column(self):
        assert describe_models([Reserved], using="reader").split("\n\n")[0] == (
            'CREATE TABLE "order" (\n'
            "  id bigint NOT NULL PRIMARY KEY,\n"
            '  "User" integer NOT NULL,\n'
            "  label varchar(10) NOT NULL\n"
            ");\n"
        )

    def test_composite_primary_key(self):
        assert describe_models([Pair], using="reader").split("\n\n")[0] == (
            "CREATE TABLE tests_pair (\n"
            "  left_id integer NOT NULL,\n"
            "  right_id integer NOT NULL,\n"
            "  PRIMARY KEY (left_id, right_id)\n"
            ");\n"
        )

    def test_multi_table_inheritance(self):
        # Only the child table's own columns.
        assert describe_models([Child], using="reader").split("\n\n")[0] == (
            "CREATE TABLE tests_child (\n"
            "  parent_ptr_id bigint NOT NULL PRIMARY KEY,\n"
            "  age integer NOT NULL\n"
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
            encode_result(["a"], [("x" * 2000,), ("y",)], more=False, max_size=1000)
        )
        assert result == {
            "columns": ["a"],
            "rows": [["x" * 100 + "… [1900 more characters]"], ["y"]],
            "truncated": True,
            "note": (
                "Long values were shortened. Select parts of them, such as with"
                " substr(), to read more."
            ),
        }

    def test_long_other_values(self):
        result = decode(
            encode_result(
                ["a", "b", "c"],
                [(b"x" * 200, ["y" * 200], {"z": "z" * 200})],
                more=False,
                max_size=1000,
            )
        )
        assert [len(value) for value in result["rows"][0]] == [123] * 3
        assert result["rows"][0][1].startswith('["yyy')
        assert result["truncated"] is True

    def test_wide_row(self):
        # Many columns share the budget, so even one row fits.
        row = tuple("x" * 200 for _ in range(20))
        raw = encode_result(["a"] * 20, [row], more=False, max_size=1000)
        # Roughly, since each shortened value gains a suffix.
        assert len(bytes(raw)) < 2000
        assert decode(raw)["rows"][0][0] == "x" * 50 + "… [150 more characters]"

    def test_not_finite(self):
        row = (
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        )
        result = decode(encode_result(["a"] * 6, [row], more=False, max_size=1000))
        assert result["rows"] == [
            ["NaN", "Infinity", "-Infinity", "NaN", "Infinity", "-Infinity"]
        ]

    def test_first_row_kept(self):
        # Even when one row is over budget, it is shown, rather than none.
        row = tuple("x" * 90 for _ in range(10))
        result = decode(
            encode_result(["a"] * 10, [row, row], more=False, max_size=1000)
        )
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

    def test_failed_transaction(self):
        pg_connection = mock.Mock(closed=False)
        pg_connection.info.transaction_status = psycopg.pq.TransactionStatus.INERROR
        _release_advisory_locks(mock.Mock(connection=pg_connection))
        pg_connection.cursor.assert_not_called()


class FetchTests(SimpleTestCase):
    def cursor(self, rows):
        rows = iter(rows)

        def fetchmany(size):
            return [row for _, row in zip(range(size), rows, strict=False)]

        return mock.Mock(fetchmany=fetchmany)

    def test_size(self):
        cursor = self.cursor([("x" * 300,)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000, deadline=1e12)
        # One row, then two more, which fit, then one, over the limit.
        assert (len(rows), more) == (4, True)

    def test_huge_first_row(self):
        cursor = self.cursor([("x" * 2000,)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000, deadline=1e12)
        assert (len(rows), more) == (1, True)

    def test_deadline(self):
        cursor = self.cursor([(1,)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000, deadline=0)
        assert (len(rows), more) == (1, True)

    def test_exact(self):
        cursor = self.cursor([(1,)] * 10)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000, deadline=1e12)
        assert (len(rows), more) == (10, False)


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
            "Without tables, list the tables sql_query can query, with what"
            " they hold. With tables, describe their columns, as CREATE TABLE"
            " statements with comments explaining the data. Call this before"
            " writing queries."
        )
        assert tools["sql_schema"]["inputSchema"]["properties"]["tables"]["anyOf"][
            0
        ] == {"type": "array", "items": {"type": "string"}}
        assert tools["sql_query"]["description"] == (
            "Run one read-only PostgreSQL SELECT query and get the result rows,"
            ' as JSON: "columns" names the columns, and "rows" holds one array'
            " of values per row. Call sql_schema to find the tables and their"
            " columns first. At most 2 rows are returned, and large results"
            " are cut short, so aggregate, filter, and sort in SQL rather than"
            " fetching everything. Queries time out after 1 seconds."
        )
        assert tools["sql_query"]["annotations"] == {
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert tools["sql_query"]["inputSchema"]["required"] == ["sql"]

    def test_schema_tables(self):
        result = self.call("sql_schema")
        assert result["content"] == [{"type": "text", "text": TABLES}]

    def test_schema_tables_empty(self):
        result = self.call("sql_schema", {"tables": []})
        assert result["content"] == [{"type": "text", "text": TABLES}]

    def test_schema_columns(self):
        # Names as written in SQL, quoted or not, and repeated.
        result = self.call(
            "sql_schema", {"tables": ["tests_widget_view", '"tests_widget_view"']}
        )
        assert result["content"] == [{"type": "text", "text": SCHEMA}]

    def test_schema_unknown(self):
        result = self.call("sql_schema", {"tables": ["tests_widget", "Nope"]})
        assert result["isError"]
        assert result["content"][0]["text"] == (
            'Unknown tables: tests_widget, "Nope". Available tables: tests_widget_view.'
        )

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

    def test_jsonb(self):
        assert self.query(
            """SELECT '{"a": [1]}'::jsonb AS j, 'NaN'::numeric AS n"""
        ) == {
            "columns": ["j", "n"],
            "rows": [[{"a": [1]}, "NaN"]],
        }

    def test_unloadable_value(self):
        assert self.query("SELECT 'infinity'::date AS d") == (
            "Query error: date too large (after year 10K): 'infinity'. Cast the"
            " column to text to read such values."
        )

    def test_privileged_role(self):
        # The postgres alias logs in as a superuser.
        with pytest.raises(ImproperlyConfigured) as excinfo:
            run_query(
                "SELECT 1", using="postgres", max_rows=1, max_size=1000, timeout=1
            )
        assert str(excinfo.value).startswith(
            "The database alias 'postgres' must log in as a role that is not a"
            " superuser,"
        )

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
                max_size=1000,
                timeout=1,
            )
        assert str(excinfo.value) == (
            "Query error: transaction read-write mode must be set before any query"
        )

    def test_table_columns(self):
        # Granted some columns of a table, the reader can read just those.
        sync_sql_grants(models=[WidgetName], reader="reader", using="postgres")
        try:
            assert run_query(
                "SELECT name FROM tests_widget ORDER BY name",
                using="reader",
                max_rows=10,
                max_size=1000,
                timeout=1,
            ) == (["name"], [("Doohickey",), ("Gizmo",), ("Sprocket",)], False)
            with pytest.raises(ToolError) as excinfo:
                run_query(
                    "SELECT price FROM tests_widget",
                    using="reader",
                    max_rows=10,
                    max_size=1000,
                    timeout=1,
                )
            assert str(excinfo.value) == (
                "Query error: permission denied for table tests_widget"
            )
        finally:
            sync_sql_grants(models=[WidgetView], reader="reader", using="postgres")

    def test_autocommit_restored(self):
        assert run_query(
            "SELECT 1", using="reader", max_rows=10, max_size=1000, timeout=1
        ) == (
            ["?column?"],
            [(1,)],
            False,
        )
        connection = connections["reader"]
        assert connection.get_autocommit()
        assert not connection.in_atomic_block


class SyncSQLGrantsTests(TestCase):
    databases = {"postgres"}

    def fetch(self, sql, params=None):
        with connections["postgres"].cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def column_privileges(self, table):
        return {
            column: self.fetch(
                "SELECT has_column_privilege('mcpz_test_reader', %s, %s, 'SELECT')",
                [table, column],
            )[0]
            for column in ["id", "name", "price", "in_stock"]
        }

    def table_privilege(self, table):
        return self.fetch(
            "SELECT has_table_privilege('mcpz_test_reader', %s, 'SELECT')", [table]
        )[0]

    def any_column_privilege(self, role, table):
        return self.fetch(
            "SELECT has_any_column_privilege(%s, %s, 'SELECT')", [role, table]
        )[0]

    def test_view(self):
        # As done by the post_migrate handler in tests.apps. Grants are per
        # column, so not on the table as a whole.
        sync_sql_grants(models=[WidgetView], reader="reader", using="postgres")
        assert self.any_column_privilege("mcpz_test_reader", "tests_widget_view")
        assert not self.table_privilege("tests_widget_view")
        assert not self.table_privilege("tests_widget")
        assert self.column_privileges("tests_widget") == {
            "id": False,
            "name": False,
            "price": False,
            "in_stock": False,
        }

    def test_columns(self):
        # Columns of models sharing a table are merged, skipping any field
        # without a column, and anything else is revoked.
        sync_sql_grants(
            models=[WidgetName, WidgetPrice], reader="reader", using="postgres"
        )
        assert self.column_privileges("tests_widget") == {
            "id": True,
            "name": True,
            "price": True,
            "in_stock": False,
        }
        assert not self.table_privilege("tests_widget")
        assert not self.any_column_privilege("mcpz_test_reader", "tests_widget_view")

    def test_revokes_stale_grants(self):
        with connections["postgres"].cursor() as cursor:
            cursor.execute("GRANT SELECT ON tests_widget TO mcpz_test_reader")
            cursor.execute(
                "GRANT SELECT (in_stock) ON tests_widget TO mcpz_test_reader"
            )
        sync_sql_grants(models=[WidgetName], reader="reader", using="postgres")
        assert not self.table_privilege("tests_widget")
        assert self.column_privileges("tests_widget") == {
            "id": True,
            "name": True,
            "price": False,
            "in_stock": False,
        }

    def test_creates_role(self):
        with mock.patch.dict(
            connections["reader"].settings_dict,
            {"USER": "mcpz_test_new", "PASSWORD": "secret"},
        ):
            sync_sql_grants(models=[WidgetView], reader="reader", using="postgres")
        assert self.fetch(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s",
            ["mcpz_test_new"],
        ) == (True, False)
        assert self.any_column_privilege("mcpz_test_new", "tests_widget_view")

    def test_not_postgresql(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            sync_sql_grants(models=[WidgetView], reader="reader", using="default")
        assert str(excinfo.value) == (
            "sync_sql_grants() needs a PostgreSQL database, but 'default' is sqlite."
        )

    def test_no_user(self):
        with (
            mock.patch.dict(connections["reader"].settings_dict, {"USER": ""}),
            pytest.raises(ImproperlyConfigured) as excinfo,
        ):
            sync_sql_grants(models=[WidgetView], reader="reader", using="postgres")
        assert str(excinfo.value) == (
            "sync_sql_grants() needs the 'reader' database alias to set USER, the"
            " role it logs in as."
        )

    def test_same_user(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            sync_sql_grants(models=[WidgetView], reader="postgres", using="postgres")
        assert str(excinfo.value).startswith(
            "The 'postgres' and 'postgres' database aliases log in as the same role,"
        )

    def test_missing_table(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            sync_sql_grants(models=[Undocumented], reader="reader", using="postgres")
        assert str(excinfo.value) == (
            "sync_sql_grants() can't find the table 'tests_undocumented'. Create"
            " it, or its view, in a migration."
        )
