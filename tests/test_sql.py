from __future__ import annotations

import datetime as dt
import ipaddress
import time
import uuid
from decimal import Decimal
from typing import Any
from unittest import mock

import msgspec.json
import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.models import Model
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)

from django_mcpz.server import MCPServer, public
from django_mcpz.sql.queries import (
    _sqlite_close,
    _watchdog,
    get_query,
)
from django_mcpz.sql.readers import SQLReader
from django_mcpz.sql.tools import (
    _fetch,
    _rough_size,
    add_sql_tools,
    describe_tables,
    encode_result,
    list_tables,
    run_query,
)
from tests.mcp import sql_postgres_reader, sql_sqlite_reader
from tests.models import (
    Child,
    Pair,
    Parent,
    Part,
    Reserved,
    Undocumented,
    Widget,
    WidgetProxy,
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

WIDGET_VIEW: dict[type[Model], list[str]] = {WidgetView: ["id", "name", "price"]}
PART: dict[type[Model], list[str]] = {
    Part: ["id", "widget_id", "sku", "size", "supplier", "rank"]
}


def make_reader(tables, using="postgres", **kwargs):
    return SQLReader(tables=tables, using=using, **kwargs)


# For tests of the database's own limits, on what queries can do beyond
# reading, which hold even for queries the checker would reject.
skip_check = mock.patch(
    "django_mcpz.sql.tools.check_query", new=lambda sql, **kwargs: sql
)


def create_widgets(using="default"):
    Widget.objects.using(using).bulk_create(
        [
            Widget(name="Sprocket", price="4.50"),
            Widget(name="Gizmo", price="12.00", in_stock=False),
            Widget(name="Doohickey", price="0.99"),
        ]
    )


class AddSQLToolsTests(SimpleTestCase):
    def add(self, **kwargs):
        server = MCPServer(name="s", version="1", auth=public)
        add_sql_tools(server, **{"reader": sql_postgres_reader, **kwargs})
        return server

    def error(self, **kwargs):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            self.add(**kwargs)
        return str(excinfo.value)

    def test_unsupported_database(self):
        connection = mock.Mock(vendor="mysql", display_name="MySQL", alias="mysql")
        with pytest.raises(ImproperlyConfigured) as excinfo:
            get_query(connection)
        assert str(excinfo.value) == (
            "The SQL tools support PostgreSQL and SQLite, not MySQL, for the"
            " database alias 'mysql'."
        )

    def test_max_rows(self):
        assert self.error(max_rows=0) == "max_rows must be at least 1."

    def test_max_size(self):
        assert self.error(max_size=999) == "max_size must be at least 1000."

    def test_timeout(self):
        assert self.error(timeout=0) == "timeout must be positive."

    def test_name_prefix_and_permission(self):
        server = self.add(name_prefix="widgets", permission=lambda request: False)
        assert list(server._tools) == ["widgets_schema", "widgets_query"]
        assert not server._tools["widgets_query"].permitted(mock.Mock())


class SQLReaderTests(SimpleTestCase):
    def error(self, tables=None, **kwargs):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            make_reader(WIDGET_VIEW if tables is None else tables, **kwargs)
        return str(excinfo.value)

    def test_reader(self):
        assert sql_postgres_reader.using == "postgres"
        assert sql_postgres_reader.view_permissions is False
        assert [table.name for table in sql_postgres_reader.tables] == [
            "tests_widget_view"
        ]
        assert sql_postgres_reader.tables[0].columns == ["id", "name", "price"]

    def test_immutable(self):
        with pytest.raises(AttributeError) as excinfo:
            sql_postgres_reader.using = "postgres"
        assert str(excinfo.value) == "SQLReader is immutable."
        with pytest.raises(AttributeError):
            del sql_postgres_reader.using

    def test_using_unknown(self):
        assert self.error(using="nope") == (
            "SQLReader using='nope' isn't a database alias in DATABASES."
        )

    def test_defaults(self):
        reader = SQLReader(tables={Widget: ["id"]})
        assert (reader.using, reader.view_permissions) == ("default", False)

    def test_no_tables(self):
        assert self.error({}) == (
            "SQLReader tables must be a dict of models to lists of field names."
        )

    def test_not_a_model(self):
        assert self.error({"tests.Widget": ["id"]}) == (
            "SQLReader tables must have models as keys, not 'tests.Widget'."
        )

    def test_fields_string(self):
        # A common slip: ("name") is a string, not a tuple.
        assert self.error({Widget: ("name")}) == (
            "SQLReader tables[tests.Widget] must be a list of field names, not 'name'."
        )

    def test_no_fields(self):
        assert self.error({Widget: []}).endswith(
            "must be a list of field names, not []."
        )

    def test_unknown_field(self):
        assert self.error({Widget: ["nope"]}) == (
            "SQLReader tables[tests.Widget] has 'nope', which isn't a field of"
            " tests.Widget."
        )

    def test_field_without_column(self):
        assert self.error({Reserved: ["nothing"]}).endswith(
            "has 'nothing', which isn't a column of tests.Reserved's table."
        )

    def test_parent_field(self):
        # The parent's fields are in its own table, not the child's.
        assert self.error({Child: ["name"]}).endswith(
            "has 'name', which isn't a column of tests.Child's table."
        )

    def test_reverse_relation(self):
        assert self.error({WidgetView: ["part"]}).endswith(
            "has 'part', which isn't a column of tests.WidgetView's table."
        )

    def test_duplicate_field(self):
        # Also by its attname.
        assert self.error({Part: ["widget", "widget_id"]}) == (
            "SQLReader tables[tests.Part] has 'widget_id' more than once."
        )

    def test_duplicate_table(self):
        assert self.error({Widget: ["id"], WidgetProxy: ["name"]}) == (
            "SQLReader tables has the table 'tests_widget' more than once. List"
            " all its fields together."
        )


class ListTablesTests(SimpleTestCase):
    @override_settings(USE_TZ=False, TIME_ZONE="Europe/London")
    def test_sqlite_local_time(self):
        reader = make_reader({Widget: ["id"]}, using="default")
        assert list_tables(reader, reader.tables).startswith(
            "Dates and times are stored as ISO 8601 text, in the time zone"
            " Europe/London, so use SQLite's date and time functions on them.\n"
        )

    def test_tables(self):
        reader = make_reader(
            {WidgetView: ["id"], Undocumented: ["id"], Reserved: ["id"], Part: ["id"]}
        )
        assert list_tables(reader, reader.tables, schema_tool="widgets_schema") == (
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


class DescribeTablesTests(SimpleTestCase):
    def describe(self, tables):
        reader = make_reader(tables)
        return describe_tables(reader, reader.tables)

    def test_view(self):
        assert self.describe(WIDGET_VIEW) == SCHEMA

    def test_some_fields(self):
        # Only the named fields, in the model's order.
        assert self.describe({Widget: ["price", "name"]}) == (
            "CREATE TABLE tests_widget (\n"
            "  name varchar(100) NOT NULL,\n"
            "  price numeric(6, 2) NOT NULL\n"
            ");\n"
        )

    def test_all_tables(self):
        # References to tables outside the described ones, but queryable.
        reader = make_reader({**WIDGET_VIEW, **PART})
        assert describe_tables(reader, reader.tables[1:]).splitlines()[4] == (
            "  widget_id bigint NOT NULL REFERENCES tests_widget_view (id),"
        )

    def test_fields(self):
        # Part references WidgetView, which is described, and Widget, which is
        # not, so only the first gets a REFERENCES.
        assert self.describe({**WIDGET_VIEW, **PART, Undocumented: ["id"]}) == (
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

    def test_reference_to_hidden_column(self):
        # No REFERENCES when the target column isn't exposed.
        assert (
            self.describe({WidgetView: ["name"], Part: ["widget"]}).splitlines()[-2]
            == "  widget_id bigint NOT NULL"
        )


class DescribeEdgeCasesTests(SimpleTestCase):
    def describe(self, tables):
        reader = make_reader(tables)
        return describe_tables(reader, reader.tables)

    def test_quoting(self):
        assert self.describe({Reserved: ["id", "user", "label"]}) == (
            'CREATE TABLE "order" (\n'
            "  id bigint NOT NULL PRIMARY KEY,\n"
            '  "User" integer NOT NULL,\n'
            "  label varchar(10) NOT NULL\n"
            ");\n"
        )

    def test_composite_primary_key(self):
        assert self.describe({Pair: ["left_id", "right_id"]}) == (
            "CREATE TABLE tests_pair (\n"
            "  left_id integer NOT NULL,\n"
            "  right_id integer NOT NULL,\n"
            "  PRIMARY KEY (left_id, right_id)\n"
            ");\n"
        )

    def test_composite_primary_key_partial(self):
        # Without all its columns, there's no primary key to describe.
        assert self.describe({Pair: ["left_id"]}) == (
            "CREATE TABLE tests_pair (\n  left_id integer NOT NULL\n);\n"
        )

    def test_multi_table_inheritance(self):
        assert self.describe({Child: ["parent_ptr", "age"]}) == (
            "CREATE TABLE tests_child (\n"
            "  parent_ptr_id bigint NOT NULL PRIMARY KEY,\n"
            "  age integer NOT NULL\n"
            ");\n"
        )
        assert self.describe({Parent: ["id", "name"]}).startswith(
            "CREATE TABLE tests_parent (\n"
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


class WatchdogTests(SimpleTestCase):
    def test_cancels(self):
        pg_connection = mock.Mock(spec=["cancel_safe"])
        with _watchdog(pg_connection, 0.01):
            time.sleep(0.1)
        pg_connection.cancel_safe.assert_called_once_with()

    def test_finished(self):
        pg_connection = mock.Mock(spec=["cancel_safe"])
        with _watchdog(pg_connection, 0.05):
            pass
        time.sleep(0.1)
        pg_connection.cancel_safe.assert_not_called()

    def test_finished_while_firing(self):
        # The timer fires as the block finishes, and waits for the lock,
        # which the block holds until it has marked itself finished.
        pg_connection = mock.Mock(spec=["cancel_safe"])
        with mock.patch("threading.Timer") as timer:
            with _watchdog(pg_connection, 1):
                cancel = timer.call_args.args[1]
            cancel()
        pg_connection.cancel_safe.assert_not_called()

    def test_old_psycopg(self):
        pg_connection = mock.Mock(spec=["cancel"])
        with _watchdog(pg_connection, 0.01):
            time.sleep(0.1)
        pg_connection.cancel.assert_called_once_with()


class FetchTests(SimpleTestCase):
    def cursor(self, rows):
        rows = iter(rows)

        def fetchmany(size):
            return [row for _, row in zip(range(size), rows, strict=False)]

        return mock.Mock(fetchmany=fetchmany)

    def test_size(self):
        cursor = self.cursor([("x" * 300,)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000)
        # One row, then two more, which fit, then one, over the limit.
        assert (len(rows), more) == (4, True)

    def test_huge_first_row(self):
        cursor = self.cursor([("x" * 2000,)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000)
        assert (len(rows), more) == (1, True)

    def test_exact(self):
        cursor = self.cursor([(1,)] * 10)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000)
        assert (len(rows), more) == (10, False)

    def test_huge_array(self):
        cursor = self.cursor([(["x" * 300, {"k": "x" * 300}],)] * 30)
        rows, more = _fetch(cursor, max_rows=100, max_size=1000)
        assert (len(rows), more) == (2, True)

    def test_rough_size(self):
        assert (
            _rough_size([1, ("ab", b"c"), {"d": "ef"}]) == 8 + 8 + 8 + 2 + 1 + 8 + 1 + 2
        )


@skip_check
class PostgresServerTests(ServerTestCase, TestCase):
    url = "/sql-postgres-mcp"
    databases = {"postgres"}

    @classmethod
    def setUpClass(cls):
        # The reader alias sees only committed data, so create it before
        # TestCase opens its transactions.
        create_widgets(using="postgres")
        cls.addClassCleanup(Widget.objects.using("postgres").all().delete)
        super().setUpClass()

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

    def test_not_a_query(self):
        assert self.query("DELETE FROM tests_widget") == (
            'Query error: syntax error at or near "DELETE"\n'
            "LINE 1: DELETE FROM tests_widget\n"
            "        ^"
        )

    def test_syntax_error(self):
        assert self.query("SELECT FROM WHERE") == (
            'Query error: syntax error at or near "WHERE"\n'
            "LINE 1: SELECT FROM WHERE\n"
            "                    ^"
        )

    def test_error_second_line(self):
        assert self.query("SELECT 1\nFROM nope") == (
            'Query error: relation "nope" does not exist\n'
            "LINE 2: FROM nope\n"
            "             ^"
        )

    def test_error_hint(self):
        assert self.query("SELECT nope()") == (
            "Query error: function nope() does not exist\n"
            "LINE 1: SELECT nope()\n"
            "               ^\n"
            "HINT:  No function matches the given name and argument types. You"
            " might need to add explicit type casts."
        )

    def test_execute_wrapper(self):
        # Queries run through Django's database layer, so its hooks see them.
        seen = []

        def wrapper(execute, sql, params, many, context):
            seen.append(sql)
            return execute(sql, params, many, context)

        with connections["postgres"].execute_wrapper(wrapper):
            self.query("SELECT 1 AS one")
        assert "SELECT 1 AS one\n" in seen

    def test_multiple_statements(self):
        assert self.query("SELECT 1; DELETE FROM tests_widget") == (
            "Query error: cannot insert multiple commands into a prepared statement"
        )

    def test_writing_with(self):
        assert self.query(
            "WITH d AS (DELETE FROM tests_widget RETURNING id) SELECT * FROM d"
        ) == (
            "Query error: DECLARE CURSOR must not contain data-modifying"
            " statements in WITH"
        )

    def test_read_write(self):
        assert self.query(
            "SELECT set_config('transaction_read_only', 'off', true)"
        ) == ("Query error: transaction read-write mode must be set before any query")

    def test_request_transaction(self):
        # TestCase runs the test in a transaction, like ATOMIC_REQUESTS, now
        # with a query run, and a change made. The query runs in its own
        # transaction, on its own connection, so doesn't see the change.
        Widget.objects.using("postgres").create(name="Uncommitted", price="1.00")
        assert self.query("SELECT count(*) AS n FROM tests_widget_view") == {
            "columns": ["n"],
            "rows": [[3]],
        }

    def test_timeout(self):
        assert self.query("SELECT pg_sleep(5)") == (
            "The query was cancelled after running for 1 seconds. Make it"
            " cheaper, for example by filtering earlier or aggregating less"
            " data."
        )

    def test_timeout_turned_off(self):
        # Turning off statement_timeout takes effect for later FETCHes, but the
        # watchdog still cancels the query.
        assert self.query(
            "SELECT set_config('statement_timeout', '0', true),"
            " pg_sleep(CASE WHEN n > 1 THEN 5 ELSE 0 END)"
            " FROM generate_series(1, 3) AS n"
        ) == (
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


@skip_check
class SQLiteServerTests(ServerTestCase, TransactionTestCase):
    # Queries run on a fresh connection, which sees only committed data.
    url = "/sql-sqlite-mcp"

    def setUp(self):
        create_widgets()

    def call(self, name, arguments=None):
        response = self.post(
            make_message("tools/call", {"name": name, "arguments": arguments or {}})
        )
        assert response.status_code == 200
        return response.json()["result"]

    def query(self, sql):
        result = self.call("sql_query", {"sql": sql})
        if result["isError"]:
            return result["content"][0]["text"]
        return result["structuredContent"]

    def test_description(self):
        response = self.post(make_message("tools/list"))
        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert tools["sql_query"]["description"].startswith(
            "Run one read-only SQLite SELECT query"
        )

    def test_tables(self):
        text = self.call("sql_schema")["content"][0]["text"]
        assert text.startswith(
            "Dates and times are stored as ISO 8601 text, in UTC, so use SQLite's"
            " date and time functions on them.\n"
        )

    def test_query(self):
        assert self.query("SELECT name, price FROM tests_widget ORDER BY price") == {
            "columns": ["name", "price"],
            "rows": [["Doohickey", 0.99], ["Sprocket", 4.5]],
            "truncated": True,
            "note": (
                "Only the first 2 rows are shown. Aggregate, filter, or add a"
                " LIMIT to see the rest."
            ),
        }

    def test_count(self):
        assert self.query("SELECT count(*) AS n FROM tests_widget_view") == {
            "columns": ["n"],
            "rows": [[3]],
        }

    def test_view(self):
        # The view reads price and in_stock behind the scenes, which is fine.
        assert self.query(
            "SELECT name FROM tests_widget_view WHERE id = (SELECT min(id) FROM"
            " tests_widget)"
        ) == {"columns": ["name"], "rows": [["Sprocket"]]}

    def test_with_view(self):
        assert self.query(
            "WITH x AS (SELECT name FROM tests_widget_view) SELECT count(*) AS n,"
            " min(name) AS name FROM x"
        ) == {"columns": ["n", "name"], "rows": [[3, "Doohickey"]]}

    def test_count_view(self):
        assert self.query("SELECT count(*) AS n FROM tests_widget_view") == {
            "columns": ["n"],
            "rows": [[3]],
        }

    def test_too_long(self):
        assert self.query("SELECT length(zeroblob(100000000)) AS n") == (
            "Query error: string or blob too big"
        )

    def test_with_and_functions(self):
        assert self.query(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r"
            " WHERE n < 3) SELECT group_concat(n) AS ns, (SELECT count(*) FROM"
            " json_each('[1, 2]')) AS js FROM r"
        ) == {"columns": ["ns", "js"], "rows": [["1,2,3", 2]]}

    def test_percent(self):
        assert self.query("SELECT '%s' LIKE '%%s' AS p") == {
            "columns": ["p"],
            "rows": [[1]],
        }

    def test_delete(self):
        # PRAGMA query_only stops writes the checker would reject.
        assert self.query("DELETE FROM tests_widget") == (
            "Query error: attempt to write a readonly database"
        )
        assert Widget.objects.count() == 3

    def test_multiple_statements(self):
        # Python's sqlite3 refuses, as an unexpected error, since the checker
        # allows only one statement.
        with self.assertLogs("django_mcpz", "ERROR"):
            assert self.query("SELECT 1; DELETE FROM tests_widget") == (
                "Tool 'sql_query' failed unexpectedly."
            )
        assert Widget.objects.count() == 3

    def test_syntax_error(self):
        assert self.query("SELECT FROM") == ('Query error: near "FROM": syntax error')

    def test_timeout(self):
        assert self.query(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r)"
            " SELECT count(*) FROM r"
        ) == (
            "The query was cancelled after running for 1 seconds. Make it"
            " cheaper, for example by filtering earlier or aggregating less"
            " data."
        )

    def test_app_connection_untouched(self):
        # The app's own authorizer, on its connection, survives queries.
        import sqlite3

        calls = []

        def authorize(*args):
            calls.append(args)
            return sqlite3.SQLITE_OK

        connection = connections["default"]
        connection.ensure_connection()
        connection.connection.set_authorizer(authorize)
        try:
            assert self.query("SELECT count(*) AS n FROM tests_widget") == {
                "columns": ["n"],
                "rows": [[3]],
            }
            Widget.objects.create(name="Thingamajig", price="1.00")
        finally:
            connection.connection.set_authorizer(None)
        assert any(call[0] == sqlite3.SQLITE_INSERT for call in calls)
        assert Widget.objects.count() == 4

    def test_execute_wrapper(self):
        # Hooks registered on the alias's connection see the query.
        seen = []

        def wrapper(execute, sql, params, many, context):
            seen.append(sql)
            return execute(sql, params, many, context)

        with connections["default"].execute_wrapper(wrapper):
            self.query("SELECT 1 AS one")
        assert "SELECT 1 AS one" in seen

    def test_private_memory_database(self):
        with (
            mock.patch.dict(connections["default"].settings_dict, {"NAME": ":memory:"}),
            pytest.raises(ImproperlyConfigured) as excinfo,
        ):
            run_query(
                "SELECT 1",
                reader=sql_sqlite_reader,
                max_rows=1,
                max_size=1000,
                timeout=1,
            )
        assert str(excinfo.value) == (
            "The SQL tools can't query the database alias 'default', a private"
            " in-memory SQLite database, which only its own connection can open."
        )

    def test_close_unconnected(self):
        # Closing a connection that never connected, after failing to.
        connection = connections.create_connection("default")
        _sqlite_close(connection)
        assert connection.connection is None


class SQLiteErrorTests(SimpleTestCase):
    def test_server_error(self):
        # Errors that are not the query's fault, such as a locked database,
        # are raised as they are.
        import sqlite3

        from django.db import OperationalError

        from django_mcpz.sql.queries import _sqlite_error

        cause = sqlite3.OperationalError("database is locked")
        cause.sqlite_errorname = "SQLITE_BUSY"
        exc = OperationalError("database is locked")
        exc.__cause__ = cause
        assert _sqlite_error(exc, "SELECT 1", 1) is exc
