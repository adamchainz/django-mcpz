from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from unittest_parametrize import ParametrizedTestCase, param, parametrize

from django_mcpz._sqlcheck import QueryNotAllowed, check_query
from django_mcpz.server import ToolError
from django_mcpz.sql.queries import FUNCTIONS
from django_mcpz.sql.tools import list_tables, run_query
from tests.mcp import sql_sqlite_reader
from tests.models import Widget
from tests.test_server import ServerTestCase, make_message
from tests.test_sql import create_widgets

TABLES = {
    "widget": ["id", "name", "price", "data"],
    "part": ["id", "widget_id", "sku"],
}


def check(sql: str, dialect: str = "postgresql") -> str:
    return check_query(
        sql, dialect=dialect, tables=TABLES, functions=FUNCTIONS[dialect]
    )


def error(sql: str, dialect: str = "postgresql") -> str:
    with pytest.raises(QueryNotAllowed) as excinfo:
        check(sql, dialect)
    return str(excinfo.value)


class CheckQueryTests(ParametrizedTestCase, SimpleTestCase):
    @parametrize(
        "sql,checked",
        [
            param(
                "select name, price from widget where price > 1"
                " order by price desc limit 10 offset 5",
                "SELECT name, price FROM widget WHERE price > 1"
                " ORDER BY price DESC LIMIT 10 OFFSET 5",
                id="select",
            ),
            param(
                "SELECT name FROM widget -- a comment",
                "SELECT name FROM widget",
                id="comment",
            ),
            param(
                "SELECT w.name, count(*) AS n FROM widget AS w JOIN part AS p"
                " ON p.widget_id = w.id GROUP BY w.name HAVING count(*) > 1"
                " ORDER BY n",
                "SELECT w.name, count(*) AS n FROM widget AS w JOIN part AS p"
                " ON p.widget_id = w.id GROUP BY w.name HAVING count(*) > 1"
                " ORDER BY n",
                id="join",
            ),
            param(
                "SELECT w.name FROM widget w LEFT JOIN part p USING (id)",
                "SELECT w.name FROM widget w LEFT JOIN part p USING(id)",
                id="using",
            ),
            param(
                "SELECT widget.name FROM widget, part WHERE part.widget_id = widget.id",
                "SELECT widget.name FROM widget, part WHERE part.widget_id = widget.id",
                id="comma_join",
            ),
            param(
                # Only widget has a readable name, so it must be that one.
                "SELECT name FROM widget, part",
                "SELECT name FROM widget, part",
                id="unqualified_unique",
            ),
            param(
                "WITH cheap AS (SELECT id, name FROM widget WHERE price < 5)"
                " SELECT name FROM cheap",
                "WITH cheap AS (SELECT id, name FROM widget WHERE price < 5)"
                " SELECT name FROM cheap",
                id="cte",
            ),
            param(
                "WITH c(a) AS (SELECT name FROM widget) SELECT a FROM c",
                "WITH c (a) AS (SELECT name FROM widget) SELECT a FROM c",
                id="cte_columns",
            ),
            param(
                # A CTE hides the table of the same name, as in the database.
                "WITH widget AS (SELECT 1 AS x) SELECT x FROM widget",
                "WITH widget AS (SELECT 1 AS x) SELECT x FROM widget",
                id="cte_hides_table",
            ),
            param(
                "SELECT s.n FROM (SELECT count(*) AS n FROM widget) AS s",
                "SELECT s.n FROM (SELECT count(*) AS n FROM widget) AS s",
                id="derived",
            ),
            param(
                "SELECT name FROM widget UNION ALL SELECT sku FROM part ORDER BY name",
                "SELECT name FROM widget UNION ALL SELECT sku FROM part ORDER BY name",
                id="union",
            ),
            param(
                "SELECT name FROM widget WHERE id IN (SELECT widget_id FROM part)",
                "SELECT name FROM widget WHERE id IN (SELECT widget_id FROM part)",
                id="in_subquery",
            ),
            param(
                # id resolves to part's, the innermost, as in the database.
                "SELECT w.name FROM widget w WHERE EXISTS"
                " (SELECT 1 FROM part p WHERE p.widget_id = w.id AND id > 1)",
                "SELECT w.name FROM widget w WHERE EXISTS"
                " (SELECT 1 FROM part p WHERE p.widget_id = w.id AND id > 1)",
                id="correlated",
            ),
            param(
                "SELECT (SELECT max(sku) FROM part) AS s",
                "SELECT (SELECT max(sku) FROM part) AS s",
                id="scalar_subquery",
            ),
            param(
                "SELECT CASE WHEN price > 1 THEN 'big' ELSE 'small' END AS size"
                " FROM widget",
                "SELECT CASE WHEN price > 1 THEN 'big' ELSE 'small' END AS size"
                " FROM widget",
                id="case",
            ),
            param(
                "SELECT CAST(price AS integer), price::text, round(price, 1)"
                " FROM widget",
                "SELECT CAST(price AS INTEGER), price::TEXT, round(price, 1)"
                " FROM widget",
                id="casts",
            ),
            param(
                "SELECT data->>'colour', data -> 'size' FROM widget",
                "SELECT data ->> 'colour', data -> 'size' FROM widget",
                id="json",
            ),
            param(
                "SELECT name, row_number() OVER (PARTITION BY price ORDER BY id"
                " ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM widget",
                "SELECT name, row_number() OVER (PARTITION BY price ORDER BY id"
                " ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM widget",
                id="window",
            ),
            param(
                "SELECT sum(price) FILTER (WHERE price > 1),"
                " count(DISTINCT name) FROM widget",
                "SELECT sum(price) FILTER (WHERE price > 1),"
                " count(DISTINCT name) FROM widget",
                id="aggregates",
            ),
            param(
                "SELECT date_trunc('month', current_date),"
                " extract(year from current_date)",
                "SELECT date_trunc('month', current_date),"
                " EXTRACT(YEAR FROM current_date)",
                id="dates",
            ),
            param(
                "SELECT name FROM widget WHERE name LIKE 'a%' AND name ILIKE 'b%'"
                " AND NOT price BETWEEN 1 AND 2 AND id IS NOT NULL"
                " AND id IN (1, 2) AND price IS DISTINCT FROM 3",
                "SELECT name FROM widget WHERE name LIKE 'a%' AND name ILIKE 'b%'"
                " AND NOT price BETWEEN 1 AND 2 AND id IS NOT NULL"
                " AND id IN (1, 2) AND price IS DISTINCT FROM 3",
                id="conditions",
            ),
            param(
                "SELECT string_agg(name, ', ' ORDER BY name) FROM widget",
                "SELECT string_agg(name, ', ' ORDER BY name) FROM widget",
                id="aggregate_order_by",
            ),
            param(
                "SELECT substring(name from 1 for 2), trim(name),"
                " position('a' in name), ceil(price), floor(price) FROM widget",
                "SELECT SUBSTRING(name FROM 1 FOR 2), TRIM(name),"
                " POSITION('a' IN name), CEIL(price), FLOOR(price) FROM widget",
                id="special_functions",
            ),
        ],
    )
    def test_allowed(self, sql, checked):
        assert check(sql) == checked

    @parametrize(
        "sql,message",
        [
            param(
                "SELECT * FROM widget",
                "SELECT * isn't allowed: name the columns to select.",
                id="star",
            ),
            param(
                "SELECT w.* FROM widget w",
                "SELECT * isn't allowed: name the columns to select.",
                id="qualified_star",
            ),
            param(
                "SELECT secret FROM widget",
                "widget has no column secret that can be queried.",
                id="unreadable_column",
            ),
            param(
                "SELECT w.secret FROM widget w",
                "w has no column secret that can be queried.",
                id="unreadable_qualified_column",
            ),
            param(
                # A whole-row reference, reading every column.
                "SELECT w FROM widget w",
                "w has no column w that can be queried.",
                id="whole_row",
            ),
            param(
                "SELECT name FROM auth_user",
                "There's no table auth_user that can be queried. Call the schema"
                " tool to list them.",
                id="unreadable_table",
            ),
            param(
                "SELECT name FROM public.widget",
                "public.widget isn't allowed: use unqualified names, without a schema.",
                id="schema",
            ),
            param(
                "SELECT x.name FROM widget",
                "There's no table or alias x in the query, for x.name.",
                id="unknown_qualifier",
            ),
            param(
                "SELECT public.widget.name FROM widget",
                "public.widget.name isn't allowed: qualify columns with just a"
                " table name or alias.",
                id="long_qualifier",
            ),
            param(
                "SELECT id FROM widget w JOIN part p ON w.id = p.widget_id",
                "The column id is ambiguous. Qualify it with its table's name or"
                " alias.",
                id="ambiguous",
            ),
            param(
                # secret could be an unreadable column of part, which the
                # database would pick over the outer widget's.
                "SELECT w.name FROM widget w WHERE EXISTS"
                " (SELECT 1 FROM part p WHERE secret = 1)",
                "p has no column secret that can be queried.",
                id="inner_unreadable",
            ),
            param(
                "SELECT w.name FROM widget w, part p WHERE secret = 1",
                "No table in FROM has a column secret that can be queried.",
                id="no_table_has_column",
            ),
            param(
                "SELECT secret",
                "There's no table for the column secret.",
                id="no_table",
            ),
            param(
                "WITH x AS (SELECT secret FROM widget) SELECT 1 FROM x",
                "widget has no column secret that can be queried.",
                id="cte_unreadable",
            ),
            param(
                "WITH x AS (SELECT 1 AS a), x AS (SELECT 2 AS a) SELECT a FROM x",
                "x is defined twice.",
                id="cte_twice",
            ),
            param(
                "WITH x(a, b) AS (SELECT 1) SELECT a FROM x",
                "The column list of x must name each of its query's columns.",
                id="cte_column_count",
            ),
            param(
                "WITH x AS (SELECT count(*) FROM widget) SELECT 1 FROM x",
                "Give each column of x a name, with AS.",
                id="cte_unnamed",
            ),
            param(
                "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r)"
                " SELECT n FROM r",
                "WITH RECURSIVE isn't allowed.",
                id="recursive",
            ),
            param(
                "SELECT s.n FROM (SELECT count(*) FROM widget) AS s",
                "Give each column of s a name, with AS.",
                id="derived_unnamed",
            ),
            param(
                "SELECT n FROM (SELECT count(*) AS n FROM widget)",
                "A subquery in FROM needs an alias.",
                id="derived_no_alias",
            ),
            param(
                "SELECT s.a FROM (SELECT 1) AS s(a)",
                "A column list in an alias isn't allowed.",
                id="alias_columns",
            ),
            param(
                "SELECT 1 FROM widget w LEFT JOIN part p USING (name)",
                "USING (name) needs name readable on both sides of the join.",
                id="using_unreadable",
            ),
            param(
                "SELECT 1 FROM widget NATURAL JOIN part",
                "NATURAL JOIN isn't allowed.",
                id="natural",
            ),
            param(
                "SELECT 1 FROM widget w, LATERAL (SELECT 1 AS a) AS x",
                "LATERAL isn't allowed.",
                id="lateral",
            ),
            param(
                "SELECT n FROM generate_series(1, 10) AS n",
                "A table function isn't allowed.",
                id="table_function",
            ),
            param(
                "SELECT name FROM widget w1, widget w1",
                "w1 appears twice in FROM. Give each an alias.",
                id="alias_twice",
            ),
            param(
                "SELECT pg_sleep(10)",
                "The function pg_sleep() isn't allowed.",
                id="pg_sleep",
            ),
            param(
                "SELECT set_config('a', 'b', true)",
                "The function set_config() isn't allowed.",
                id="set_config",
            ),
            param(
                "SELECT row_to_json(w) FROM widget w",
                "The function row_to_json() isn't allowed.",
                id="row_to_json",
            ),
            param(
                "SELECT pg_catalog.count(*) FROM widget",
                "pg_catalog.count isn't allowed: use unqualified names, without a"
                " schema.",
                id="qualified_function",
            ),
            param(
                "SELECT max(*) FROM widget",
                "Those arguments to max() aren't allowed.",
                id="star_argument",
            ),
            param(
                # PostgreSQL's Unicode escapes, which the parser doesn't read
                # as a name.
                r'SELECT U&"\0073et_config"(1)',
                "The operator & isn't allowed.",
                id="unicode_escape",
            ),
            param(
                r"SELECT E'\'' AS x, name FROM widget",
                r"The value E'\'' isn't allowed: use plain numbers and"
                " 'single-quoted' strings.",
                id="escape_string",
            ),
            param(
                "SELECT $$x$$",
                "The value $$x$$ isn't allowed: use plain numbers and"
                " 'single-quoted' strings.",
                id="dollar_quoted",
            ),
            param(
                "SELECT 'pg_class'::regclass",
                "Casting to REGCLASS isn't allowed.",
                id="regclass",
            ),
            param(
                "SELECT name ~ 'x' FROM widget",
                "The operator ~ isn't allowed.",
                id="operator",
            ),
            param(
                "SELECT - - 1, ~1",
                "The operator ~ isn't allowed.",
                id="unary_operator",
            ),
            param(
                "SELECT interval '1 day'",
                "The expression INTERVAL '1 day' isn't allowed.",
                id="interval",
            ),
            param(
                "SELECT name FROM widget LIMIT (SELECT 1)",
                "(SELECT 1) must be a number.",
                id="limit_subquery",
            ),
            param(
                "SELECT name FROM widget ORDER BY secret",
                "widget has no column secret that can be queried.",
                id="order_by_unreadable",
            ),
            param(
                # PostgreSQL resolves GROUP BY names as input columns first.
                "SELECT name AS secret FROM widget GROUP BY secret",
                "GROUP BY secret isn't allowed, as it names an output column."
                " Repeat its expression, or give its position.",
                id="group_by_alias",
            ),
            param(
                "SELECT name FROM widget GROUP BY secret",
                "widget has no column secret that can be queried.",
                id="group_by_unreadable",
            ),
            param(
                "SELECT name FROM widget UNION SELECT sku FROM part ORDER BY sku",
                "ORDER BY sku isn't allowed here: name an output column, or give"
                " its position.",
                id="union_order_by",
            ),
            param(
                "SELECT DISTINCT ON (name) name FROM widget",
                "DISTINCT ON isn't allowed.",
                id="distinct_on",
            ),
            param(
                "SELECT name FROM widget FOR UPDATE",
                "Locking isn't allowed.",
                id="for_update",
            ),
            param(
                "SELECT name INTO x FROM widget",
                "SELECT INTO isn't allowed.",
                id="into",
            ),
            param(
                "SELECT name FROM widget FETCH FIRST 1 ROWS ONLY",
                "FETCH isn't allowed.",
                id="fetch",
            ),
            param(
                "VALUES (1)",
                "Only SELECT queries are allowed, optionally combined with UNION,"
                " INTERSECT, or EXCEPT.",
                id="values",
            ),
            param(
                "DELETE FROM widget",
                "Only SELECT queries are allowed.",
                id="delete",
            ),
            param(
                "SELECT 1; SELECT 2",
                "Only a single statement is allowed.",
                id="statements",
            ),
            param(
                "SELECT 1 +",
                "sql parser error: Expected: an expression, found: EOF",
                id="syntax_error",
            ),
        ],
    )
    def test_rejected(self, sql, message):
        assert error(sql) == message

    def test_sqlite(self):
        assert check(
            "SELECT strftime('%Y', price), json_extract(data, '$.a'),"
            " group_concat(name) FROM widget LIMIT 5, 10",
            "sqlite",
        ) == (
            "SELECT strftime('%Y', price), json_extract(data, '$.a'),"
            " group_concat(name) FROM widget LIMIT 5, 10"
        )

    def test_sqlite_case_insensitive(self):
        # SQLite ignores case, even of quoted names.
        assert check('SELECT "Name" FROM "WIDGET"', "sqlite") == (
            'SELECT "Name" FROM "WIDGET"'
        )

    def test_postgresql_quoted_case(self):
        assert error('SELECT "Name" FROM widget') == (
            'widget has no column "Name" that can be queried.'
        )

    def test_sqlite_functions(self):
        assert error("SELECT date_trunc('month', current_date)", "sqlite") == (
            "The function date_trunc() isn't allowed."
        )
        assert error("SELECT repeat('x', 3)", "sqlite") == (
            "The function repeat() isn't allowed."
        )

    def test_sqlite_ilike(self):
        assert error("SELECT name FROM widget WHERE name ILIKE 'a'", "sqlite") == (
            "ILIKE isn't allowed."
        )

    def test_sqlite_schema(self):
        assert error("SELECT name FROM main.widget", "sqlite") == (
            "main.widget isn't allowed: use unqualified names, without a schema."
        )

    def test_unknown_dialect(self):
        with pytest.raises(QueryNotAllowed) as excinfo:
            check_query("SELECT 1", dialect="mysql", tables={}, functions=[])
        assert str(excinfo.value) == 'Unknown dialect "mysql".'

    def test_functions_iterable(self):
        assert (
            check_query(
                "SELECT count(*) FROM widget",
                dialect="sqlite",
                tables=TABLES,
                functions=iter(["COUNT"]),
            )
            == "SELECT count(*) FROM widget"
        )


class RunQueryCheckedTests(TransactionTestCase):
    # Queries run on a fresh connection, which sees only committed data.
    def setUp(self):
        create_widgets()

    def run_query(self, sql, **kwargs):
        return run_query(
            sql,
            reader=sql_sqlite_reader,
            max_rows=10,
            max_size=1000,
            timeout=1,
            **kwargs,
        )

    def test_runs_checked_query(self):
        assert self.run_query(
            "select name from tests_widget where price < 1 -- cheap"
        ) == (["name"], [("Doohickey",)], False)

    def test_tables(self):
        # Only the given tables, of the reader's.
        with pytest.raises(ToolError) as excinfo:
            self.run_query(
                "SELECT name FROM tests_widget",
                tables=[
                    table
                    for table in sql_sqlite_reader.tables
                    if table.name == "tests_widget_view"
                ],
            )
        assert str(excinfo.value) == (
            "Query not allowed: There's no table tests_widget that can be"
            " queried. Call the schema tool to list them."
        )


class ListTablesNoneTests(SimpleTestCase):
    def test_none(self):
        assert "Tables:\nNone. You don't have permission to view any.\n" in (
            list_tables(sql_sqlite_reader, [])
        )


class PermissionsServerTests(ServerTestCase, TransactionTestCase):
    # With the reader's view_permissions, the tools see the tables the user
    # has the view permission for.
    url = "/sql-permissions-mcp"

    def setUp(self):
        create_widgets()

    def log_in(self, *permissions: str, superuser: bool = False) -> None:
        user = User.objects.create(username="user", is_superuser=superuser)
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="tests", codename__in=permissions
            )
        )
        self.client.force_login(user)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        response = self.post(
            make_message("tools/call", {"name": name, "arguments": arguments or {}})
        )
        assert response.status_code == 200
        result = response.json()["result"]
        if result["isError"] or "structuredContent" not in result:
            return result["content"][0]["text"]
        return result["structuredContent"]

    def tables(self) -> list[str]:
        text: str = self.call("sql_schema")
        listed = text.split("Tables:\n")[1].split("\n\n")[0]
        return listed.splitlines()

    def test_anonymous(self):
        assert self.tables() == ["None. You don't have permission to view any."]
        assert self.call("sql_query", {"sql": "SELECT name FROM tests_widget"}) == (
            "Query not allowed: There's no table tests_widget that can be"
            " queried. Call the schema tool to list them."
        )

    def test_some(self):
        self.log_in("view_widget")
        assert self.tables() == ["- tests_widget"]
        assert self.call(
            "sql_query", {"sql": "SELECT count(*) AS n FROM tests_widget"}
        ) == {"columns": ["n"], "rows": [[3]]}
        assert self.call(
            "sql_query", {"sql": "SELECT name FROM tests_widget_view"}
        ) == (
            "Query not allowed: There's no table tests_widget_view that can be"
            " queried. Call the schema tool to list them."
        )
        assert self.call("sql_schema", {"tables": ["tests_widget_view"]}) == (
            "Unknown tables: tests_widget_view. Available tables: tests_widget."
        )

    def test_superuser(self):
        self.log_in(superuser=True)
        assert self.tables() == [
            "- tests_widget",
            "- tests_widget_view: Widgets for sale, without their stock levels.",
        ]

    def test_references_only_visible(self):
        # No REFERENCES to tables the user can't see. tests_widget_view has no
        # foreign keys, so check the description is just its own.
        self.log_in("view_widgetview")
        assert "REFERENCES" not in self.call(
            "sql_schema", {"tables": ["tests_widget_view"]}
        )


class PostgresCheckedTests(ServerTestCase, TestCase):
    # The checker in front of PostgreSQL, running the query as checked.
    url = "/sql-postgres-mcp"
    databases = {"postgres"}

    @classmethod
    def setUpClass(cls):
        # The reader alias sees only committed data.
        create_widgets(using="postgres")
        cls.addClassCleanup(Widget.objects.using("postgres").all().delete)
        super().setUpClass()

    def query(self, sql: str) -> Any:
        response = self.post(
            make_message("tools/call", {"name": "sql_query", "arguments": {"sql": sql}})
        )
        result = response.json()["result"]
        if result["isError"]:
            return result["content"][0]["text"]
        return result["structuredContent"]

    def test_checked(self):
        assert self.query(
            "select name from tests_widget_view where price < 1 -- cheap"
        ) == {"columns": ["name"], "rows": [["Doohickey"]]}

    def test_not_allowed(self):
        assert self.query("SELECT pg_sleep(5)") == (
            "Query not allowed: The function pg_sleep() isn't allowed."
        )
