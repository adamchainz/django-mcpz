from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from unittest import mock

import msgspec.json
import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import EmptyResultSet, ImproperlyConfigured
from django.db import DatabaseError, connection
from django.db.models import Model, QuerySet
from django.http import HttpRequest
from django.test import RequestFactory, SimpleTestCase, TestCase

from django_mcpz import orm
from django_mcpz.jsonrpc import INVALID_PARAMS
from django_mcpz.orm import (
    QueryParams,
    _is_timeout,
    _timeout,
    _TimeoutState,
    add_query_tool,
)
from django_mcpz.server import PROTOCOL_VERSION, MCPServer, ToolError, public
from tests.mcp import header_user_auth
from tests.models import Category, PremiumWidget, Sprocket, Supplier, Tag, Widget
from tests.test_server import ServerTestCase, make_message


def make_server(**kwargs: Any) -> MCPServer:
    server = MCPServer(name="orm-test", version="1.0.0", auth=header_user_auth)
    add_query_tool(server, **kwargs)
    return server


def call(
    server: MCPServer,
    arguments: dict[str, Any],
    *,
    user: str | None = "admin",
    name: str = "query",
) -> dict[str, Any]:
    """Call a tool on a server directly, returning the tools/call result."""
    request = RequestFactory().post(
        "/mcp",
        data=msgspec.json.encode(
            make_message("tools/call", {"name": name, "arguments": arguments})
        ),
        content_type="application/json",
        headers={
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
            **({"X-User": user} if user is not None else {}),
        },
    )
    response = server(request)
    data = msgspec.json.decode(response.content)
    assert "error" not in data, data
    result: dict[str, Any] = data["result"]
    return result


def text_of(result: dict[str, Any]) -> str:
    assert result["content"][0]["type"] == "text"
    text: str = result["content"][0]["text"]
    return text


class QueryToolTestCase(TestCase):
    """Users and data for the query tool tests."""

    @classmethod
    def setUpTestData(cls):
        User.objects.create_superuser("admin", password="secret")
        viewer = User.objects.create_user("viewer")
        viewer.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="tests",
                codename__in=["view_widget", "view_category", "view_tag"],
            )
        )
        User.objects.create_user("nobody")

        toys = Category.objects.create(name="Toys")
        tools = Category.objects.create(name="Tools")
        fun = Tag.objects.create(name="fun")
        acme = Supplier.objects.create(name="Acme", secret_rate="0.20")
        yoyo = Widget.objects.create(
            name="Yo-yo", price="1.50", category=toys, supplier=acme
        )
        yoyo.tags.add(fun)
        Widget.objects.create(name="Hammer", price="9.99", category=tools)
        Widget.objects.create(name="Ball", price="2.00", in_stock=False, colour="blue")
        PremiumWidget.objects.create(name="Drill", price="99.00", warranty_years=3)
        Sprocket.objects.create(widget=yoyo, size=3)

    def query(self, code: str, *, user: str = "admin", **kwargs: Any) -> str:
        server = self.server(**kwargs)
        return text_of(call(server, {"query": code}, user=user))

    def error(self, code: str, *, user: str = "admin", **kwargs: Any) -> str:
        server = self.server(**kwargs)
        result = call(server, {"query": code}, user=user)
        assert result["isError"], result
        return text_of(result)

    def server(self, **kwargs: Any) -> MCPServer:
        kwargs.setdefault("models", ["tests", "auth.User", "auth.Group"])
        kwargs.setdefault("hidden_fields", ["tests.Supplier.secret_rate"])
        return make_server(**kwargs)


class RegistrationTests(SimpleTestCase):
    def test_unknown_app(self):
        with pytest.raises(ImproperlyConfigured, match="unknown app"):
            make_server(models=["nonexistent"])

    def test_unknown_model(self):
        with pytest.raises(ImproperlyConfigured, match="unknown model"):
            make_server(models=["tests.Nonexistent"])

    def test_bad_entry(self):
        with pytest.raises(ImproperlyConfigured, match="app labels, model labels"):
            make_server(models=[123])

    def test_abstract_model(self):
        class Abstract(Model):
            class Meta:
                abstract = True
                app_label = "tests"

        with pytest.raises(ImproperlyConfigured, match="app labels, model labels"):
            make_server(models=[Abstract])

    def test_no_models(self):
        with pytest.raises(ImproperlyConfigured, match="at least one model"):
            make_server(models=[])

    def test_model_classes(self):
        server = make_server(models=[Widget, "tests.Widget"])

        tool = server._tools["query"]
        assert tool.definition["annotations"] == {
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert tool.definition["inputSchema"]["properties"].keys() == {
            "query",
            "app",
        }

    def test_bad_timeout(self):
        with pytest.raises(ImproperlyConfigured, match="timeout must be positive"):
            make_server(models=["tests"], timeout=0)

    def test_bad_max_rows(self):
        with pytest.raises(ImproperlyConfigured, match="max_rows"):
            make_server(models=["tests"], max_rows=0)

    def test_bad_using(self):
        with pytest.raises(ImproperlyConfigured, match="unknown database"):
            make_server(models=["tests"], using="replica")

    def test_bad_hidden_field_format(self):
        with pytest.raises(ImproperlyConfigured, match="app_label.Model.field"):
            make_server(models=["tests"], hidden_fields=["price"])

    def test_unknown_hidden_field(self):
        with pytest.raises(ImproperlyConfigured, match="unknown model or field"):
            make_server(models=["tests"], hidden_fields=["tests.Widget.nope"])

    def test_unknown_hidden_model(self):
        with pytest.raises(ImproperlyConfigured, match="unknown model or field"):
            make_server(models=["tests"], hidden_fields=["tests.Nope.name"])

    def test_name_and_title(self):
        server = make_server(models=["tests"], name="db", title="Database")

        assert server._tools["db"].definition["title"] == "Database"

    def test_description(self):
        server = make_server(models=["tests"], instructions="Prices are in GBP.")

        description = server._tools["query"].definition["description"]
        assert "from tests.models import Category" in description
        assert "at most 200 rows" in description
        assert "time out after 30 seconds" in description
        assert description.endswith("\n\nPrices are in GBP.")

    def test_description_no_timeout(self):
        server = make_server(models=["tests"], timeout=None)

        description = server._tools["query"].definition["description"]
        assert "time out" not in description


class PermissionTests(QueryToolTestCase, ServerTestCase):
    url = "/orm-mcp"

    def list_tools(self, username: str | None) -> list[str]:
        response = self.post(make_message("tools/list"), headers={"X-User": username})
        assert response.status_code == 200
        return [tool["name"] for tool in response.json()["result"]["tools"]]

    def test_hidden_without_permissions(self):
        assert self.list_tools("nobody") == []

    def test_hidden_anonymous(self):
        assert self.list_tools(None) == []

    def test_listed_with_some_permissions(self):
        assert self.list_tools("viewer") == ["query"]

    def test_call_denied(self):
        response = self.post(
            make_message("tools/call", {"name": "query", "arguments": {}}),
            headers={"X-User": "nobody"},
        )

        self.assert_error(response, INVALID_PARAMS)

    def test_overview_limited(self):
        response = self.post(
            make_message("tools/call", {"name": "query", "arguments": {}}),
            headers={"X-User": "viewer"},
        )

        text = text_of(response.json()["result"])
        assert text.endswith("\n\ntests: Category, Tag, Widget")

    def test_custom_tool_permission(self):
        server = make_server(
            models=["tests"], permission=lambda request: request.user.is_superuser
        )

        assert server._tools["query"].permitted(self.request_for("admin"))
        assert not server._tools["query"].permitted(self.request_for("viewer"))

    def test_custom_model_permission(self):
        server = make_server(
            models=["tests"],
            model_permission=lambda request, model: model is Tag,
        )

        text = text_of(call(server, {}, user="nobody"))
        assert text.endswith("\n\ntests: Tag")

    def test_missing_request_user(self):
        server = MCPServer(name="orm-test", version="1.0.0", auth=public)
        add_query_tool(server, models=["tests"])
        request = RequestFactory().post("/mcp")

        with pytest.raises(ImproperlyConfigured, match="nothing set request.user"):
            server._tools["query"].permitted(request)

    def test_unpermitted_model_import(self):
        message = self.error(
            "from tests.models import Supplier\nresult = Supplier.objects.values()",
            user="viewer",
        )

        assert message.startswith(
            "Line 1: cannot import 'Supplier' from tests.models: it is not a model"
            " available to query"
        )

    def test_join_to_unpermitted_model(self):
        message = self.error(
            'from tests.models import Widget\nresult = Widget.objects.values("supplier__name")',
            user="viewer",
        )

        assert message.startswith(
            "The query reads tests.Supplier, which is not available to query."
        )

    def test_filter_on_unpermitted_model(self):
        message = self.error(
            'from tests.models import Widget\nresult = Widget.objects.filter(supplier__name="Acme").values("name")',
            user="viewer",
        )

        assert message.startswith("The query reads tests.Supplier")

    def test_order_by_unpermitted_model(self):
        # Ordering joins are only set up when the query is compiled.
        message = self.error(
            'from tests.models import Widget\nresult = Widget.objects.order_by("supplier__name").values("name")',
            user="viewer",
        )

        assert message.startswith("The query reads tests.Supplier")

    def test_default_ordering_through_unpermitted_model(self):
        server = make_server(
            models=["tests.Sprocket"],
            model_permission=lambda request, model: True,
        )

        result = call(
            server,
            {
                "query": "from tests.models import Sprocket\nresult = Sprocket.objects.values('size')"
            },
        )

        assert result["isError"]
        assert text_of(result).startswith("The query reads tests.Widget")

    def test_default_ordering_cleared(self):
        server = make_server(
            models=["tests.Sprocket"],
            model_permission=lambda request, model: True,
        )

        result = call(
            server,
            {
                "query": "from tests.models import Sprocket\nresult = Sprocket.objects.order_by().values('size')"
            },
        )

        assert text_of(result) == "[{'size': 3}]"

    def test_subquery_on_unpermitted_model(self):
        # The model is permitted at registration, but not for this user.
        message = self.error(
            "from tests.models import Widget, Supplier\n"
            "from django.db.models import OuterRef, Subquery\n"
            "result = Widget.objects.annotate(s=Subquery(Supplier.objects.filter(pk=OuterRef('supplier_id')).values('name')[:1])).values('s')",
            user="viewer",
        )

        assert message.startswith("Line 1: cannot import 'Supplier'")

    def test_nested_query_tables_checked(self):
        server = make_server(
            models=["tests.Widget", "tests.Sprocket"],
            model_permission=lambda request, model: True,
        )

        result = call(
            server,
            {
                "query": (
                    "from tests.models import Widget, Sprocket\n"
                    "from django.db.models import OuterRef, Subquery\n"
                    "result = Widget.objects.annotate(s=Subquery(Sprocket.objects.filter(widget=OuterRef('pk')).order_by('widget__category__name').values('size')[:1])).values('s')"
                )
            },
        )

        assert result["isError"]
        assert text_of(result).startswith("The query reads tests.Category")

    def test_many_to_many_through_table_allowed(self):
        text = self.query(
            'from tests.models import Widget\nresult = Widget.objects.filter(tags__name="fun").values("name")',
            user="viewer",
        )

        assert text == "[{'name': 'Yo-yo'}]"

    def test_parent_table_allowed(self):
        server = make_server(
            models=["tests.PremiumWidget"],
            model_permission=lambda request, model: True,
        )

        text = text_of(
            call(
                server,
                {
                    "query": "from tests.models import PremiumWidget\nresult = PremiumWidget.objects.values('name', 'warranty_years')"
                },
            )
        )

        assert text == "[{'name': 'Drill', 'warranty_years': 3}]"

    def test_unknown_table(self):
        server = make_server(models=["tests.Widget"])
        tool = server._tools["query"]
        evaluator = orm._Evaluator(
            models=[Widget], hidden=frozenset(), table_models={}, using=None
        )

        with pytest.raises(ToolError, match="The query reads some_table"):
            evaluator._check_table("some_table")
        assert tool is not None

    def request_for(self, username: str) -> HttpRequest:
        request = RequestFactory().post("/mcp")
        request.user = User.objects.get(username=username)
        return request


class DiscoveryTests(QueryToolTestCase):
    def test_overview(self):
        server = make_server(models=["tests", "auth.User", "auth.Group"])

        text = text_of(call(server, {}))

        assert text == (
            "Apps and models available to query. Call again with app=<label> for"
            " each model's fields and import line, or with query=<code> to run"
            " a query.\n\n"
            "auth: Group, User\n"
            "tests: Category, PremiumWidget, Sprocket, Supplier, Tag, Widget"
        )

    def test_overview_no_models(self):
        server = make_server(
            models=["tests"],
            permission=lambda request: True,
            model_permission=lambda request, model: False,
        )

        result = call(server, {})

        assert result["isError"]
        assert text_of(result) == "No models are available to query."

    def test_both_arguments(self):
        server = make_server(models=["tests"])

        result = call(server, {"query": "x", "app": "tests"})

        assert result["isError"]
        assert text_of(result) == "Pass either query or app, not both."

    def test_app(self):
        server = make_server(
            models=["tests.Widget", "tests.Category"],
            hidden_fields=["tests.Widget.price"],
        )

        text = text_of(call(server, {"app": "tests"}))

        assert text == (
            "Models in the tests app, with their fields as name: type. Follow"
            ' relations with double underscores, as in .values("category__name")'
            ' or .filter(category__name="Toys").\n'
            "\n"
            "Category\n"
            "  from tests.models import Category\n"
            "  Managers: objects\n"
            "  Fields:\n"
            "    id: BigAutoField, primary key\n"
            "    name: CharField, unique — Shown in the navigation.\n"
            "  Reverse relations:\n"
            "    widgets: reverse of tests.Widget.category, nullable\n"
            "\n"
            "Widget\n"
            "  from tests.models import Widget\n"
            "  Managers: objects\n"
            "  Fields:\n"
            "    id: BigAutoField, primary key\n"
            "    name: CharField\n"
            "    in_stock: BooleanField\n"
            "    colour: CharField, choices: 'red' (Red), 'blue' (Blue)"
            " — The main colour.\n"
            "    category: ForeignKey to tests.Category, nullable\n"
            "    supplier: ForeignKey to tests.Supplier (not available), nullable\n"
            "    tags: ManyToManyField to tests.Tag (not available)\n"
            "  Reverse relations:\n"
            "    premiumwidget: reverse of tests.PremiumWidget.widget_ptr"
            " (not available), nullable\n"
            "    sprocket: reverse of tests.Sprocket.widget (not available),"
            " nullable"
        )

    def test_app_docstrings(self):
        server = make_server(
            models=["tests.Category", "tests.Sprocket", "auth.User"], docstrings=True
        )

        text = text_of(call(server, {"app": "tests"}))
        assert "  Managers: objects\n  A grouping of widgets, shown" in text
        # Sprocket has a generated docstring, which is skipped, and no
        # reverse relations.
        assert text.endswith(
            "Sprocket\n"
            "  from tests.models import Sprocket\n"
            "  Managers: objects\n"
            "  Fields:\n"
            "    id: BigAutoField, primary key\n"
            "    widget: ForeignKey to tests.Widget (not available)\n"
            "    size: PositiveIntegerField"
        )

        # User's docstring is inherited from AbstractUser, since Django
        # generates one for User itself.
        text = text_of(call(server, {"app": "auth"}))
        assert "\n  Users within the Django authentication system" in text
        assert "    password:" not in text

    def test_app_docstring_generated(self):
        # Widget has no docstring, so Django generates "Widget(id, ...)".
        assert orm._model_docstring(Widget) is None

    def test_app_many_choices(self):
        field = mock.Mock(
            spec=["name", "related_model", "auto_created", "concrete", "null"],
            related_model=None,
            auto_created=False,
            concrete=True,
            null=False,
        )
        field.name = "size"
        field.flatchoices = [(i, str(i)) for i in range(25)]
        field.primary_key = False
        field.unique = False
        field.help_text = ""

        line = orm._QueryTool(
            models=[Widget],
            model_permission=lambda request, model: True,
            hidden=frozenset(),
            using=None,
            timeout=None,
            max_rows=1,
            docstrings=False,
        )._describe_field(field, [Widget])

        assert line.startswith("    size: Mock, choices: 0 (0), 1 (1), ")
        assert line.endswith(", 19 (19), ...")

    def test_app_unknown(self):
        server = make_server(models=["tests"])

        result = call(server, {"app": "auth"})

        assert result["isError"]
        assert text_of(result) == (
            "No app 'auth' is available to query. Available apps: tests."
        )

    def test_app_unknown_no_models(self):
        server = make_server(
            models=["tests"],
            permission=lambda request: True,
            model_permission=lambda request, model: False,
        )

        result = call(server, {"app": "tests"})

        assert text_of(result) == (
            "No app 'tests' is available to query. Available apps: none."
        )


class QueryTests(QueryToolTestCase):
    def test_values(self):
        text = self.query(
            "from tests.models import Widget\n"
            'result = Widget.objects.filter(in_stock=True).values("name", "price")'
        )

        assert text == (
            "[{'name': 'Drill', 'price': Decimal('99.00')},\n"
            " {'name': 'Hammer', 'price': Decimal('9.99')},\n"
            " {'name': 'Yo-yo', 'price': Decimal('1.50')}]"
        )

    def test_bare_expression_result(self):
        text = self.query(
            'from tests.models import Widget\nWidget.objects.values_list("name", flat=True)[:2]'
        )

        assert text == "['Ball', 'Drill']"

    def test_intermediate_names(self):
        text = self.query(
            "from tests.models import Widget\n"
            "cheap = Widget.objects.filter(price__lt=5)\n"
            'result = cheap.order_by("-price").values_list("name", "price", named=True)'
        )

        assert text == (
            "[Row(name='Ball', price=Decimal('2.00')),"
            " Row(name='Yo-yo', price=Decimal('1.50'))]"
        )

    def test_import_alias(self):
        text = self.query(
            "from tests.models import Widget as W\nresult = W.objects.count()"
        )

        assert text == "4"

    def test_import_from_app_models_module(self):
        # Models may be imported from their app's models module, even if
        # defined elsewhere.
        with mock.patch.object(Widget, "__module__", "tests.models.widgets"):
            text = self.query(
                "from tests.models import Widget\nresult = Widget.objects.count()"
            )

        assert text == "4"

    def test_annotate_and_aggregate(self):
        text = self.query(
            "from tests.models import Category\n"
            "from django.db.models import Count, Sum\n"
            'result = Category.objects.annotate(n=Count("widgets")).values("name", "n").order_by("name")'
        )
        assert text == "[{'name': 'Tools', 'n': 1}, {'name': 'Toys', 'n': 1}]"

        text = self.query(
            "from tests.models import Widget\n"
            "from django.db.models import Count, Sum\n"
            'result = Widget.objects.aggregate(Count("id"), total=Sum("price"))'
        )
        # SQLite's decimal precision varies, so check the shape only.
        assert text.startswith("{'total': Decimal('112.49")
        assert text.endswith("'), 'id__count': 4}")

    def test_aggregate_probe_checks_joins(self):
        message = self.error(
            "from tests.models import Widget\n"
            "from django.db.models import Max\n"
            'result = Widget.objects.aggregate(Max("supplier__secret_rate"))'
        )

        assert message.startswith("The field tests.Supplier.secret_rate")

    def test_expressions(self):
        text = self.query(
            "from tests.models import Widget\n"
            "from django.db.models import F, Q, Value\n"
            "from django.db.models.functions import Lower, Concat\n"
            "result = Widget.objects.filter(Q(in_stock=False) | ~Q(colour='red'))"
            ".annotate(double=F('price') * 2 + Value(0), lower=Lower('name'))"
            ".order_by(F('price').desc(nulls_last=True))"
            ".values('lower', 'double', minus=-F('price'))"
        )

        assert text == (
            "[{'lower': 'ball', 'double': Decimal('4'), 'minus': Decimal('-2')}]"
        )

    def test_dates_and_times(self):
        text = self.query(
            "from datetime import date, timedelta\n"
            "from decimal import Decimal\n"
            "from uuid import UUID\n"
            "from django.utils.timezone import now\n"
            "from django.db.models.functions import Now\n"
            "from django.contrib.auth.models import User\n"
            "result = User.objects.filter(date_joined__lte=now() + timedelta(days=1),"
            " date_joined__date__gte=date(2000, 1, 1)).exclude(last_login__gt=Now())"
            ".values_list('username', flat=True).order_by('username')"
        )

        assert text == "['admin', 'nobody', 'viewer']"

    def test_subquery(self):
        text = self.query(
            "from tests.models import Widget, Category\n"
            "from django.db.models import OuterRef, Subquery, Exists\n"
            "cheapest = Widget.objects.filter(category=OuterRef('pk')).order_by('price').values('name')[:1]\n"
            "result = Category.objects.annotate(cheapest=Subquery(cheapest))"
            ".filter(Exists(Widget.objects.filter(category=OuterRef('pk'))))"
            ".values('name', 'cheapest')"
        )

        assert text == (
            "[{'name': 'Tools', 'cheapest': 'Hammer'}, {'name': 'Toys', 'cheapest': 'Yo-yo'}]"
        )

    def test_in_subquery(self):
        text = self.query(
            "from tests.models import Widget, Tag\n"
            "result = Widget.objects.filter(tags__in=Tag.objects.filter(name='fun')).values('name')"
        )

        assert text == "[{'name': 'Yo-yo'}]"

    def test_union(self):
        text = self.query(
            "from tests.models import Widget, Category\n"
            "result = Widget.objects.order_by().values('name').union(Category.objects.order_by().values('name')).order_by('name')"
        )

        assert text == (
            "[{'name': 'Ball'},\n {'name': 'Drill'},\n {'name': 'Hammer'},\n"
            " {'name': 'Tools'},\n {'name': 'Toys'},\n {'name': 'Yo-yo'}]"
        )

    def test_union_checks_combined_queries(self):
        message = self.error(
            "from tests.models import Widget, Supplier\n"
            "result = Widget.objects.order_by().values('name').union(Supplier.objects.order_by().values('secret_rate'))"
        )

        assert message.startswith("The field tests.Supplier.secret_rate")

    def test_dates(self):
        text = self.query(
            "from django.contrib.auth.models import User\n"
            "result = User.objects.dates('date_joined', 'year')"
        )

        assert text == f"[datetime.date({dt.date.today().year}, 1, 1)]"

    def test_count_exists_first_last_get(self):
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.count()"
            )
            == "4"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.filter(name='X').exists()"
            )
            == "False"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values('name').first()"
            )
            == "{'name': 'Ball'}"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values('name').last()"
            )
            == "{'name': 'Yo-yo'}"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True).get(price=2)"
            )
            == "'Ball'"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.filter(name='X').values('name').first()"
            )
            == "None"
        )

    def test_every_executing_call_checked(self):
        # Each executing call is checked on its own, whatever came before.
        message = self.error(
            "from tests.models import Widget\n"
            "first = Widget.objects.values('name').get(name='Ball')\n"
            "result = Widget.objects.values('name').get(supplier__secret_rate=0)"
        )

        assert message.startswith("The field tests.Supplier.secret_rate")

    def test_get_probe_checks_joins(self):
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('name').get(supplier__secret_rate=0)"
        )

        assert message.startswith("The field tests.Supplier.secret_rate")

    def test_latest_earliest(self):
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values('name').latest('price')"
            )
            == "{'name': 'Drill'}"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values('name').earliest('price', 'name')"
            )
            == "{'name': 'Yo-yo'}"
        )

    def test_latest_get_latest_by(self):
        with mock.patch.object(Widget._meta, "get_latest_by", "price"):
            text = self.query(
                "from tests.models import Widget\nresult = Widget.objects.values('name').latest()"
            )

        assert text == "{'name': 'Drill'}"

    def test_latest_no_fields(self):
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('name').latest()"
        )

        assert message.startswith("ValueError: earliest() and latest() require")

    def test_latest_probe_checks_joins(self):
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('name').latest('supplier__secret_rate')"
        )

        assert message.startswith("The field tests.Supplier.secret_rate")

    def test_get_does_not_exist(self):
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('name').get(name='X')"
        )

        assert message == "DoesNotExist: Widget matching query does not exist."

    def test_field_error(self):
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('nope')"
        )

        assert message.startswith("FieldError: Cannot resolve keyword 'nope'")

    def test_distinct_fields(self):
        # DISTINCT ON is PostgreSQL-only, so Django refuses it here.
        message = self.error(
            "from tests.models import Widget\nresult = Widget.objects.distinct('name').values('name')"
        )

        assert message == (
            "NotSupportedError: DISTINCT ON fields is not supported by this"
            " database backend"
        )

    def test_distinct_fields_checked(self):
        evaluator = orm._Evaluator(
            models=[Widget, Supplier],
            hidden=frozenset({Supplier._meta.get_field("secret_rate")}),
            table_models={},
            using=None,
        )
        query = Widget.objects.values("name").query
        query.add_distinct_fields("supplier__secret_rate")
        # Skip the table check, to reach the DISTINCT ON check.
        evaluator.table_models = {
            "tests_widget": [Widget],
            "tests_supplier": [Supplier],
        }

        with pytest.raises(ToolError, match="secret_rate is not available"):
            evaluator._check_query(query)

    def test_empty_result_set(self):
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.filter(pk__in=[]).values('name')"
        )

        assert text == "[]"

    def test_empty_result_set_when_compiling(self):
        # Django skips running a query whose compilation finds it can match
        # nothing, so there is nothing to check.
        evaluator = orm._Evaluator(
            models=[], hidden=frozenset(), table_models={}, using=None
        )

        with mock.patch(
            "django.db.models.sql.compiler.SQLCompiler.pre_sql_setup",
            side_effect=EmptyResultSet,
        ):
            evaluator._check_query(Widget.objects.values("name").query)

    def test_tuple_and_set_literals(self):
        text = self.query(
            "from tests.models import Widget\n"
            "result = Widget.objects.filter(name__in=('Ball', 'Drill')).exclude(name__in={'Drill'}).values_list('name', flat=True)"
        )

        assert text == "['Ball']"

    def test_row_cap(self):
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True)",
            max_rows=2,
        )

        assert text == (
            "['Ball', 'Drill']\n\n"
            "The query has more rows than the 2 shown. For the next page, send"
            " it again sliced [2:4], or narrow it."
        )

    def test_row_cap_next_page(self):
        # The next page continues from where the query's own slice starts.
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True)[1:100]",
            max_rows=2,
        )

        assert text == (
            "['Drill', 'Hammer']\n\n"
            "The query has more rows than the 2 shown. For the next page, send"
            " it again sliced [3:5], or narrow it."
        )

    def test_row_cap_last_page(self):
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True)[2:4]",
            max_rows=2,
        )

        assert text == "['Hammer', 'Yo-yo']"

    def test_slicing(self):
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True)[1:3]"
            )
            == "['Drill', 'Hammer']"
        )
        assert (
            self.query(
                "from tests.models import Widget\nresult = Widget.objects.values_list('name', flat=True)[3:]"
            )
            == "['Yo-yo']"
        )

    def test_using(self):
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.count()",
            using="default",
        )

        assert text == "4"

    def test_no_timeout(self):
        text = self.query(
            "from tests.models import Widget\nresult = Widget.objects.count()",
            timeout=None,
        )

        assert text == "4"


class RejectionTests(QueryToolTestCase):
    def test_syntax_error(self):
        assert self.error("result = (").startswith("Syntax error on line 1:")

    def test_null_byte(self):
        assert self.error("result = 1\x00") == "The query contains a null byte."

    def test_empty(self):
        assert self.error("   ") == "The query is empty."

    def test_too_long(self):
        assert self.error("#" * 10_001) == "The query is longer than 10000 characters."

    def test_no_models(self):
        server = make_server(
            models=["tests"],
            permission=lambda request: True,
            model_permission=lambda request, model: False,
        )

        result = call(server, {"query": "result = 1"})

        assert text_of(result) == "No models are available to query."

    def test_no_result(self):
        assert self.error("from tests.models import Widget") == (
            "End the query by assigning to result, for example:"
            " result = Model.objects.values('id', 'name')"
        )

    def test_result_not_last(self):
        assert (
            self.error("result = 1\nx = 2")
            == "Assign to result in the last statement of the query."
        )

    def test_expression_not_last(self):
        assert self.error("1\nresult = 2").startswith(
            "Line 1: an expression statement does nothing here."
        )

    def test_import_statement(self):
        assert self.error("import os").startswith(
            "Line 1: use the form 'from module import name'"
        )

    def test_relative_import(self):
        assert (
            self.error("from . import models")
            == "Line 1: relative imports are not allowed."
        )

    def test_star_import(self):
        assert (
            self.error("from tests.models import *")
            == "Line 1: import names explicitly."
        )

    def test_disallowed_module(self):
        assert self.error("from os import system").startswith(
            "Line 1: cannot import 'system' from os: it is not a model"
        )

    def test_disallowed_name(self):
        assert self.error("from django.db.models import Func").startswith(
            "Line 1: cannot import 'Func' from django.db.models. Available: Avg, "
        )

    def test_missing_module(self):
        with mock.patch.dict(orm.ALLOWED_IMPORTS, {"nope": frozenset({"x"})}):
            message = self.error("from nope import x")

        assert message == "Line 1: nope.x is not available on this server."

    def test_missing_name(self):
        with mock.patch.dict(orm.ALLOWED_IMPORTS, {"datetime": frozenset({"nope"})}):
            message = self.error("from datetime import nope")

        assert message == "Line 1: datetime.nope is not available on this server."

    def test_other_statements(self):
        assert self.error("for x in []: pass").startswith(
            "Line 1: For statements are not allowed."
        )

    def test_multiple_targets(self):
        assert self.error("a = b = 1") == "Line 1: assign to a single name."

    def test_tuple_target(self):
        assert self.error("a, b = 1, 2") == "Line 1: assign to a single name."

    def test_bytes_literal(self):
        assert self.error("result = b'x'") == "Line 1: bytes literals are not allowed."

    def test_unknown_name(self):
        assert self.error("result = Widget").startswith(
            "Line 1: unknown name 'Widget'."
        )

    def test_dict_unpacking(self):
        assert self.error("result = {**{}}") == "Line 1: ** unpacking is not allowed."

    def test_comparison(self):
        assert (
            self.error("result = 1 < 2")
            == "Line 1: Compare expressions are not allowed in queries."
        )

    def test_lambda(self):
        assert (
            self.error("result = lambda: 1")
            == "Line 1: Lambda expressions are not allowed in queries."
        )

    def test_unknown_manager(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.DoesNotExist"
        ) == ("Line 2: Widget.DoesNotExist is not available. Managers: objects.")

    def test_write_method(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.all().delete()"
        ).startswith("Line 2: QuerySet.delete() is not allowed.")

    def test_create(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.create(name='x')"
        ).startswith("Line 2: QuerySet.create() is not allowed.")

    def test_raw(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.raw('select 1')"
        ).startswith("Line 2: QuerySet.raw() is not allowed.")

    def test_extra(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.extra(where=['1=1'])"
        ).startswith("Line 2: QuerySet.extra() is not allowed.")

    def test_attribute_on_other(self):
        assert (
            self.error("result = 'x'.upper")
            == "Line 1: accessing .upper on a str is not allowed."
        )

    def test_attribute_on_instance(self):
        assert (
            self.error(
                "from tests.models import Widget\nw = Widget.objects.first()\nresult = w.delete"
            )
            == "Line 3: accessing .delete on a Widget instance is not allowed."
        )

    def test_attribute_on_expression(self):
        assert self.error(
            "from django.db.models import F\nresult = F('x').resolve_expression"
        ) == ("Line 2: accessing .resolve_expression on a F is not allowed.")

    def test_call_model(self):
        assert self.error("from tests.models import Widget\nresult = Widget()") == (
            "Line 2: calling Widget() is not allowed. Query through Widget.objects instead."
        )

    def test_call_other(self):
        assert self.error("result = 'x'()") == "Line 1: calling a str is not allowed."

    def test_call_class(self):
        assert (
            self.error(
                "from tests.models import Widget\nresult = Widget.objects.first().__class__()"
            )
            == "Line 2: accessing .__class__ on a Widget instance is not allowed."
        )

    def test_star_args(self):
        assert self.error("from django.db.models import Q\nresult = Q(*[])") == (
            "Line 2: Starred expressions are not allowed in queries."
        )

    def test_kwargs_unpacking(self):
        assert self.error("from django.db.models import Q\nresult = Q(**{})") == (
            "Line 2: ** unpacking is not allowed."
        )

    def test_function_keyword(self):
        assert (
            self.error(
                "from tests.models import Widget\nfrom django.db.models.functions import Upper\n"
                "result = Widget.objects.annotate(u=Upper('name', function='pg_sleep')).values('u')"
            )
            == "Line 3: the function argument is not allowed."
        )

    def test_private_keyword(self):
        # Django validates _connector itself, but private arguments are
        # never for callers.
        assert (
            self.error(
                "from django.db.models import Q\nresult = Q(name='x', _connector='OR')"
            )
            == "Line 2: the _connector argument is not allowed."
        )

    def test_template_keyword(self):
        assert (
            self.error(
                "from django.db.models import Count\nresult = Count('id', template='x')"
            )
            == "Line 2: the template argument is not allowed."
        )

    def test_index(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('id')[0]"
        ).startswith("Line 2: slice querysets with non-negative literal bounds")

    def test_negative_slice(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('id')[-1:]"
        ).startswith("Line 2: slice querysets")

    def test_step_slice(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('id')[::2]"
        ).startswith("Line 2: slice querysets")

    def test_bool_slice(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('id')[:True]"
        ).startswith("Line 2: slice querysets")

    def test_subscript_other(self):
        assert (
            self.error("result = [1][0]")
            == "Line 1: subscripting a list is not allowed."
        )

    def test_unary_minus_string(self):
        assert (
            self.error("result = -'x'")
            == "Line 1: unary minus applies to numbers and expressions only."
        )

    def test_unary_minus_bool(self):
        assert (
            self.error("result = -True")
            == "Line 1: unary minus applies to numbers and expressions only."
        )

    def test_unary_not(self):
        assert self.error("result = not 1") == "Line 1: Not is not allowed here."

    def test_invert_number(self):
        assert self.error("result = ~1") == "Line 1: Invert is not allowed here."

    def test_pow(self):
        assert (
            self.error("result = 2 ** 100")
            == "Line 1: the Pow operator is not allowed."
        )

    def test_plain_arithmetic(self):
        assert self.error("result = 'a' * 1000") == (
            "Line 1: operators apply to expressions, such as F() and Q(), and to dates, not to plain values."
        )

    def test_result_queryset_without_values(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.all()"
        ) == (
            "End the query with .values(...) or .values_list(...) to choose the fields to return."
        )

    def test_result_instance(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.first()"
        ) == (
            "The result contains model instances. End the query with .values(...)"
            " or .values_list(...) to choose the fields to return."
        )

    def test_result_instances_in_dict(self):
        assert self.error(
            "from tests.models import Widget\nresult = {'w': [Widget.objects.first()]}"
        ).startswith("The result contains model instances.")

    def test_result_method(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.count"
        ) == ("The result is a method. Call it.")

    def test_result_expression(self):
        assert self.error("from django.db.models import F\nresult = F('x')").startswith(
            "The result is a F, not query results."
        )

    def test_result_class(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget"
        ).startswith("The result is the class Widget, not query results.")

    def test_result_plain_values(self):
        assert self.query("result = [1, 'a', None]") == "[1, 'a', None]"

    def test_hidden_field_values(self):
        assert self.error(
            "from django.contrib.auth.models import User\nresult = User.objects.values('password')"
        ) == (
            "The field auth.User.password is not available. Name the fields to"
            " return with .values(...) or .values_list(...)."
        )

    def test_hidden_field_values_all(self):
        assert self.error(
            "from django.contrib.auth.models import User\nresult = User.objects.values()"
        ).startswith("The field auth.User.password is not available.")

    def test_hidden_field_filter(self):
        assert self.error(
            "from django.contrib.auth.models import User\nresult = User.objects.filter(password__startswith='p').values('username')"
        ).startswith("The field auth.User.password is not available.")

    def test_hidden_field_order_by(self):
        assert self.error(
            "from django.contrib.auth.models import User\nresult = User.objects.order_by('password').values('username')"
        ).startswith("The field auth.User.password is not available.")

    def test_hidden_field_annotation(self):
        assert self.error(
            "from django.contrib.auth.models import User\nfrom django.db.models import F\n"
            "result = User.objects.annotate(p=F('password')).values('username')"
        ).startswith("The field auth.User.password is not available.")

    def test_hidden_field_count(self):
        assert self.error(
            "from django.contrib.auth.models import User\nfrom django.db.models import Count\n"
            "result = User.objects.values('username').annotate(n=Count('password', distinct=True))"
        ).startswith("The field auth.User.password is not available.")

    def test_unexpected_exception(self):
        assert self.error(
            "from tests.models import Widget\nresult = Widget.objects.values('name').get('x')"
        ).startswith("ValueError: not enough values to unpack")


class TimeoutTests(QueryToolTestCase):
    def test_sqlite_timeout(self):
        with (
            mock.patch.object(orm, "SQLITE_PROGRESS_INTERVAL", 1),
            mock.patch(
                "django_mcpz.orm.time.monotonic",
                side_effect=[0.0, 100.0, 200.0, 300.0, 400.0],
            ),
        ):
            message = self.error(
                "from tests.models import Widget\nresult = Widget.objects.values('name')",
                timeout=1,
            )

        assert message == (
            "The query took longer than 1 seconds and was cancelled. Narrow it"
            " with filters or a slice."
        )

    def test_sqlite_handler_cleared(self):
        state = _TimeoutState()
        with (
            mock.patch.object(orm, "SQLITE_PROGRESS_INTERVAL", 1),
            _timeout(connection, 30, state),
        ):
            assert Widget.objects.count() == 4

        # A slow-looking clock no longer interrupts anything.
        with mock.patch("django_mcpz.orm.time.monotonic", return_value=1e12):
            assert Widget.objects.count() == 4
        assert not state.timed_out

    def test_postgresql(self):
        fake = mock.MagicMock(vendor="postgresql", alias="default")
        cursor = fake.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("0",)

        with _timeout(fake, 2.5, _TimeoutState()):
            pass

        assert cursor.execute.call_args_list == [
            mock.call("SELECT current_setting('statement_timeout')"),
            mock.call("SELECT set_config('statement_timeout', %s, true)", ["2500"]),
            mock.call("SELECT set_config('statement_timeout', %s, true)", ["0"]),
        ]

    def test_postgresql_error_leaves_transaction_to_reset(self):
        fake = mock.MagicMock(vendor="postgresql", alias="default")
        cursor = fake.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("0",)

        with pytest.raises(DatabaseError), _timeout(fake, 0.0001, _TimeoutState()):
            raise DatabaseError("canceled")

        assert cursor.execute.call_args_list == [
            mock.call("SELECT current_setting('statement_timeout')"),
            mock.call("SELECT set_config('statement_timeout', %s, true)", ["1"]),
        ]

    def test_mysql(self):
        fake = mock.MagicMock(vendor="mysql", mysql_is_mariadb=False)
        cursor = fake.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (0,)

        with pytest.raises(DatabaseError), _timeout(fake, 30, _TimeoutState()):
            raise DatabaseError("timed out")

        assert cursor.execute.call_args_list == [
            mock.call("SELECT @@SESSION.max_execution_time"),
            mock.call("SET SESSION max_execution_time = %s", ["30000"]),
            mock.call("SET SESSION max_execution_time = %s", [0]),
        ]

    def test_mariadb(self):
        fake = mock.MagicMock(vendor="mysql", mysql_is_mariadb=True)
        cursor = fake.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (Decimal("0.000000"),)

        with _timeout(fake, 30, _TimeoutState()):
            pass

        assert cursor.execute.call_args_list == [
            mock.call("SELECT @@SESSION.max_statement_time"),
            mock.call("SET SESSION max_statement_time = %s", ["30.0"]),
            mock.call("SET SESSION max_statement_time = %s", [Decimal("0.000000")]),
        ]

    def test_other_vendor(self):
        fake = mock.MagicMock(vendor="oracle")

        with _timeout(fake, 30, _TimeoutState()):
            pass

        fake.cursor.assert_not_called()

    def test_is_timeout(self):
        def wrapped(cause: BaseException) -> DatabaseError:
            exc = DatabaseError("x")
            exc.__cause__ = cause
            return exc

        class Psycopg3Error(Exception):
            sqlstate = "57014"

        class Psycopg2Error(Exception):
            pgcode = "57014"

        state = _TimeoutState()
        assert not _is_timeout(wrapped(Exception("other")), state)
        assert not _is_timeout(DatabaseError("no cause"), state)
        assert _is_timeout(wrapped(Psycopg3Error()), state)
        assert _is_timeout(wrapped(Psycopg2Error()), state)
        assert _is_timeout(wrapped(Exception(3024, "MySQL")), state)
        assert _is_timeout(wrapped(Exception(1969, "MariaDB")), state)
        assert not _is_timeout(wrapped(Exception(1045, "denied")), state)
        state.timed_out = True
        assert _is_timeout(DatabaseError("no cause"), state)

    def test_database_error_relayed(self):
        with mock.patch.object(
            QuerySet, "__iter__", side_effect=DatabaseError("disk full")
        ):
            message = self.error(
                "from tests.models import Widget\nresult = Widget.objects.values('name')"
            )

        assert message == "DatabaseError: disk full"


class ParamsTests(SimpleTestCase):
    def test_defaults(self):
        params = QueryParams()

        assert params.query is None
        assert params.app is None
