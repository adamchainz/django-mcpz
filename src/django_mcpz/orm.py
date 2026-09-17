"""
A tool that runs read-only Django ORM queries written by the calling model.

The model sends Python code, such as:

    from shop.models import Order
    from django.db.models import Count

    result = Order.objects.values("status").annotate(n=Count("id"))

The code is parsed, never exec()'d or eval()'d: a small interpreter evaluates
the syntax tree, allowing only imports, assignments, literals, and calls,
and checks every attribute access and call against the object it runs on.
Model classes expose their managers, querysets expose read-only methods, and
expressions expose asc() and desc(). Everything else is refused, so writes,
raw SQL, and arbitrary Python cannot happen.

Before any SQL runs, the query is compiled and every table it joins, including
through ordering, subqueries, and many-to-many tables, is checked against the
models the caller may view, and every column against the hidden fields.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import importlib
import inspect
import operator
import pprint
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated, Any

import msgspec
from django.apps import apps
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import (
    EmptyResultSet,
    FieldDoesNotExist,
    FullResultSet,
    ImproperlyConfigured,
)
from django.db import DatabaseError, connections, router, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Field, Model, Q
from django.db.models.expressions import Col, Combinable
from django.db.models.manager import BaseManager
from django.db.models.query import (
    FlatValuesListIterable,
    NamedValuesListIterable,
    QuerySet,
    ValuesIterable,
    ValuesListIterable,
)
from django.db.models.sql import Query
from django.http import HttpRequest

from django_mcpz.server import MCPServer, ToolError

# Names importable from each module. Base classes such as Func, Aggregate,
# Expression, and RawSQL are deliberately absent: they take a SQL function
# name or template as an argument, which would be raw SQL.
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "django.db.models": frozenset(
        {
            "F",
            "Q",
            "Value",
            "OuterRef",
            "Subquery",
            "Exists",
            "Case",
            "When",
            "ExpressionWrapper",
            "Window",
            "RowRange",
            "ValueRange",
            "OrderBy",
            "Count",
            "Sum",
            "Avg",
            "Min",
            "Max",
            "StdDev",
            "Variance",
            # Field classes, for output_field arguments.
            "BigIntegerField",
            "BooleanField",
            "CharField",
            "DateField",
            "DateTimeField",
            "DecimalField",
            "DurationField",
            "FloatField",
            "IntegerField",
            "JSONField",
            "TextField",
            "TimeField",
            "UUIDField",
        }
    ),
    "django.db.models.functions": frozenset(
        {
            "Cast",
            "Coalesce",
            "Collate",
            "Greatest",
            "JSONObject",
            "Least",
            "NullIf",
            "Extract",
            "ExtractDay",
            "ExtractHour",
            "ExtractIsoWeekDay",
            "ExtractIsoYear",
            "ExtractMinute",
            "ExtractMonth",
            "ExtractQuarter",
            "ExtractSecond",
            "ExtractWeek",
            "ExtractWeekDay",
            "ExtractYear",
            "Now",
            "Trunc",
            "TruncDate",
            "TruncDay",
            "TruncHour",
            "TruncMinute",
            "TruncMonth",
            "TruncQuarter",
            "TruncSecond",
            "TruncTime",
            "TruncWeek",
            "TruncYear",
            "Abs",
            "ACos",
            "ASin",
            "ATan",
            "ATan2",
            "Ceil",
            "Cos",
            "Cot",
            "Degrees",
            "Exp",
            "Floor",
            "Ln",
            "Log",
            "Mod",
            "Pi",
            "Power",
            "Radians",
            "Random",
            "Round",
            "Sign",
            "Sin",
            "Sqrt",
            "Tan",
            "Chr",
            "Concat",
            "Left",
            "Length",
            "Lower",
            "LPad",
            "LTrim",
            "MD5",
            "Ord",
            "Repeat",
            "Replace",
            "Reverse",
            "Right",
            "RPad",
            "RTrim",
            "SHA1",
            "SHA224",
            "SHA256",
            "SHA384",
            "SHA512",
            "StrIndex",
            "Substr",
            "Trim",
            "Upper",
            "CumeDist",
            "DenseRank",
            "FirstValue",
            "Lag",
            "LastValue",
            "Lead",
            "NthValue",
            "Ntile",
            "PercentRank",
            "Rank",
            "RowNumber",
        }
    ),
    "django.contrib.postgres.aggregates": frozenset(
        {
            "ArrayAgg",
            "BitAnd",
            "BitOr",
            "BitXor",
            "BoolAnd",
            "BoolOr",
            "JSONBAgg",
            "StringAgg",
            "Corr",
            "CovarPop",
            "RegrAvgX",
            "RegrAvgY",
            "RegrCount",
            "RegrIntercept",
            "RegrR2",
            "RegrSlope",
            "RegrSXX",
            "RegrSXY",
            "RegrSYY",
        }
    ),
    "django.contrib.postgres.search": frozenset(
        {
            "SearchVector",
            "SearchQuery",
            "SearchRank",
            "SearchHeadline",
            "TrigramSimilarity",
            "TrigramWordSimilarity",
            "TrigramDistance",
            "TrigramWordDistance",
            "TrigramStrictWordSimilarity",
            "TrigramStrictWordDistance",
        }
    ),
    "datetime": frozenset({"date", "datetime", "time", "timedelta"}),
    "decimal": frozenset({"Decimal"}),
    "uuid": frozenset({"UUID"}),
    "django.utils.timezone": frozenset({"now"}),
}

# QuerySet methods that read, or build a query that reads. Writes, raw SQL,
# extra(), and methods that only make sense for model instances are absent.
QUERYSET_METHODS = frozenset(
    {
        "all",
        "filter",
        "exclude",
        "annotate",
        "alias",
        "order_by",
        "reverse",
        "distinct",
        "values",
        "values_list",
        "dates",
        "datetimes",
        "none",
        "union",
        "intersection",
        "difference",
        "get",
        "first",
        "last",
        "latest",
        "earliest",
        "count",
        "exists",
        "aggregate",
    }
)
# The subset that runs a query when called.
_EXECUTING_METHODS = frozenset(
    {"get", "first", "last", "latest", "earliest", "count", "exists", "aggregate"}
)
_EXPRESSION_METHODS = frozenset({"asc", "desc"})

# Keyword arguments that Func subclasses accept as raw SQL.
_FORBIDDEN_KEYWORDS = frozenset({"function", "template", "arg_joiner"})

_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
# Arithmetic is allowed when at least one operand is one of these, so
# expressions can be built, but not to compute plain Python values, which
# could be arbitrarily large.
_ARITHMETIC_TYPES = (Combinable, Q, dt.date, dt.time, dt.timedelta)

_VALUES_ITERABLES = (
    ValuesIterable,
    ValuesListIterable,
    FlatValuesListIterable,
    NamedValuesListIterable,
)

MAX_QUERY_LENGTH = 10_000

# How many SQLite virtual machine instructions between timeout checks.
SQLITE_PROGRESS_INTERVAL = 10_000

# Docstrings Django generates for models without one: "Name(field, field)".
_AUTO_DOCSTRING_RE = re.compile(r"\A\w+\([\w, ]*\)\Z")


class QueryParams(msgspec.Struct):
    query: Annotated[
        str | None,
        msgspec.Meta(
            description=(
                "Python code that imports models and expressions and assigns"
                " a read-only ORM query to `result`."
            )
        ),
    ] = None
    app: Annotated[
        str | None,
        msgspec.Meta(description="An app label, to list its models and their fields."),
    ] = None


@dataclass(frozen=True)
class _Method:
    """A method the calling code may call, bound to its receiver."""

    receiver: Any
    name: str


@dataclass
class _TimeoutState:
    timed_out: bool = False


def _describe(value: Any) -> str:
    """Name a value's kind, without repr(), which runs querysets."""
    if isinstance(value, type):
        return f"the class {value.__name__}"
    if isinstance(value, Model):
        return f"a {type(value).__name__} instance"
    return f"a {type(value).__name__}"


def _view_permission(request: HttpRequest, model: type[Model]) -> bool:
    try:
        user = request.user
    except AttributeError:
        raise ImproperlyConfigured(
            "The query tool checks request.user's view permissions, but nothing"
            " set request.user. Set it in the server's auth callable, use"
            " Django's AuthenticationMiddleware, or pass model_permission."
        ) from None
    return user.has_perm(f"{model._meta.app_label}.view_{model._meta.model_name}")


def _resolve_models(models: Iterable[str | type[Model]]) -> list[type[Model]]:
    resolved: list[type[Model]] = []
    entry: Any
    for entry in models:
        found: list[type[Model]]
        if isinstance(entry, str):
            if "." in entry:
                try:
                    found = [apps.get_model(entry)]
                except LookupError:
                    raise ImproperlyConfigured(
                        f"models entry {entry!r} names an unknown model."
                    ) from None
            else:
                try:
                    found = list(apps.get_app_config(entry).get_models())
                except LookupError:
                    raise ImproperlyConfigured(
                        f"models entry {entry!r} names an unknown app."
                    ) from None
        elif (
            isinstance(entry, type)
            and issubclass(entry, Model)
            and not entry._meta.abstract
        ):
            found = [entry]
        else:
            raise ImproperlyConfigured(
                f"models entries must be app labels, model labels, or model"
                f" classes, not {entry!r}."
            )
        for model in found:
            if model not in resolved:
                resolved.append(model)
    if not resolved:
        raise ImproperlyConfigured("models must name at least one model.")
    return resolved


def _resolve_hidden_fields(
    models: list[type[Model]], hidden_fields: Iterable[str]
) -> frozenset[Field[Any, Any]]:
    hidden: set[Field[Any, Any]] = set()
    for model in models:
        # Password hashes are never useful to a language model.
        if issubclass(model, AbstractBaseUser):
            hidden.add(model._meta.get_field("password"))
    for label in hidden_fields:
        parts = label.split(".")
        if len(parts) != 3:
            raise ImproperlyConfigured(
                f"hidden_fields entry {label!r} must be 'app_label.Model.field'."
            )
        try:
            model = apps.get_model(f"{parts[0]}.{parts[1]}")
            field = model._meta.get_field(parts[2])
        except (LookupError, FieldDoesNotExist):
            raise ImproperlyConfigured(
                f"hidden_fields entry {label!r} names an unknown model or field."
            ) from None
        hidden.add(field)
    return frozenset(hidden)


def _model_docstring(model: type[Model]) -> str | None:
    """The model's docstring, or the nearest inherited one, if written by hand."""
    for klass in model.__mro__[: model.__mro__.index(Model)]:
        doc = klass.__dict__.get("__doc__")
        if doc and not _AUTO_DOCSTRING_RE.match(doc):
            return inspect.cleandoc(doc)
    return None


@contextlib.contextmanager
def _timeout(
    connection: BaseDatabaseWrapper, timeout: float, state: _TimeoutState
) -> Iterator[None]:
    """
    Limit the statements run on the connection to timeout seconds.

    PostgreSQL's statement_timeout is set locally in a transaction, which
    reverts it on rollback, as happens after a timeout. MySQL and MariaDB use
    a session variable, restored afterwards. SQLite has no such setting, so a
    progress handler interrupts the statement instead.
    """
    if connection.vendor == "postgresql":
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                (previous,) = cursor.fetchone()
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [str(max(1, round(timeout * 1000)))],
                )
            yield
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)", [previous]
                )
    elif connection.vendor == "mysql":
        if connection.mysql_is_mariadb:  # type: ignore[attr-defined]
            variable, value = "max_statement_time", str(float(timeout))
        else:
            variable, value = "max_execution_time", str(max(1, round(timeout * 1000)))
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT @@SESSION.{variable}")
            (previous,) = cursor.fetchone()
            cursor.execute(f"SET SESSION {variable} = %s", [value])
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"SET SESSION {variable} = %s", [previous])
    elif connection.vendor == "sqlite":
        connection.ensure_connection()
        deadline = time.monotonic() + timeout

        def handler() -> int:
            if time.monotonic() >= deadline:
                state.timed_out = True
                return 1
            return 0

        connection.connection.set_progress_handler(handler, SQLITE_PROGRESS_INTERVAL)
        try:
            yield
        finally:
            connection.connection.set_progress_handler(None, 0)
    else:
        yield


def _is_timeout(exc: DatabaseError, state: _TimeoutState) -> bool:
    if state.timed_out:
        return True
    cause = exc.__cause__
    # psycopg 3 and psycopg2 report the SQLSTATE differently.
    code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if code == "57014":  # query_canceled
        return True
    args: tuple[Any, ...] = getattr(cause, "args", ())
    # MySQL ER_QUERY_TIMEOUT and MariaDB ER_STATEMENT_TIMEOUT.
    return bool(args) and args[0] in (3024, 1969)


@dataclass
class _Evaluator:
    """Evaluate one query's code against the models the caller may see."""

    models: list[type[Model]]
    hidden: frozenset[Field[Any, Any]]
    table_models: dict[str, list[type[Model]]]
    using: str | None
    namespace: dict[str, Any] = dataclass_field(default_factory=dict)
    callables: list[Any] = dataclass_field(default_factory=list)

    def run(self, source: str) -> Any:
        if "\x00" in source:
            raise ToolError("The query contains a null byte.")
        try:
            tree = ast.parse(source, "<query>", mode="exec")
        except SyntaxError as exc:
            raise ToolError(f"Syntax error on line {exc.lineno}: {exc.msg}") from None
        if not tree.body:
            raise ToolError("The query is empty.")
        result: Any = None
        last_index = len(tree.body) - 1
        for index, statement in enumerate(tree.body):
            if isinstance(statement, ast.ImportFrom):
                self._import(statement)
            elif isinstance(statement, ast.Import):
                raise ToolError(
                    f"Line {statement.lineno}: use the form"
                    " 'from module import name' for imports."
                )
            elif isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(
                    statement.targets[0], ast.Name
                ):
                    raise ToolError(
                        f"Line {statement.lineno}: assign to a single name."
                    )
                name = statement.targets[0].id
                value = self._eval(statement.value)
                if name == "result":
                    if index != last_index:
                        raise ToolError(
                            "Assign to result in the last statement of the query."
                        )
                    result = value
                else:
                    self.namespace[name] = value
            elif isinstance(statement, ast.Expr):
                if index != last_index:
                    raise ToolError(
                        f"Line {statement.lineno}: an expression statement does"
                        " nothing here. Assign it to a name."
                    )
                result = self._eval(statement.value)
            else:
                raise ToolError(
                    f"Line {statement.lineno}: {type(statement).__name__}"
                    " statements are not allowed. A query is imports and"
                    " assignments, ending with result = ..."
                )
        last = tree.body[last_index]
        if not (
            isinstance(last, ast.Expr)
            or (
                isinstance(last, ast.Assign)
                and isinstance(last.targets[0], ast.Name)
                and last.targets[0].id == "result"
            )
        ):
            raise ToolError(
                "End the query by assigning to result, for example:"
                " result = Model.objects.values('id', 'name')"
            )
        return result

    # Statements

    def _import(self, node: ast.ImportFrom) -> None:
        if node.level or node.module is None:
            raise ToolError(f"Line {node.lineno}: relative imports are not allowed.")
        for alias in node.names:
            if alias.name == "*":
                raise ToolError(f"Line {node.lineno}: import names explicitly.")
            value = self._resolve_import(node.module, alias.name, node)
            self.namespace[alias.asname or alias.name] = value
            if not isinstance(value, type) or not issubclass(value, Model):
                self.callables.append(value)

    def _resolve_import(self, module: str, name: str, node: ast.ImportFrom) -> Any:
        names = ALLOWED_IMPORTS.get(module)
        if names is not None:
            if name not in names:
                raise ToolError(
                    f"Line {node.lineno}: cannot import {name!r} from {module}."
                    f" Available: {', '.join(sorted(names))}."
                )
            try:
                value = getattr(importlib.import_module(module), name)
            except (ImportError, AttributeError):
                raise ToolError(
                    f"Line {node.lineno}: {module}.{name} is not available on"
                    " this server."
                ) from None
            return value
        for model in self.models:
            models_module = model._meta.app_config.models_module
            if model.__name__ == name and module in (
                model.__module__,
                models_module.__name__ if models_module is not None else None,
            ):
                return model
        raise ToolError(
            f"Line {node.lineno}: cannot import {name!r} from {module}: it is"
            " not a model available to query, or the module is not allowed."
            " Call this tool with no arguments to list the available models,"
            " and use the import lines it shows."
        )

    # Expressions

    def _eval(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool, type(None))):
                return node.value
            raise ToolError(
                f"Line {node.lineno}: {type(node.value).__name__} literals are"
                " not allowed."
            )
        if isinstance(node, ast.Name):
            try:
                return self.namespace[node.id]
            except KeyError:
                raise ToolError(
                    f"Line {node.lineno}: unknown name {node.id!r}. Import it, or"
                    " assign it first."
                ) from None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = [self._eval(element) for element in node.elts]
            if isinstance(node, ast.List):
                return items
            if isinstance(node, ast.Tuple):
                return tuple(items)
            return set(items)
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    raise ToolError(f"Line {node.lineno}: ** unpacking is not allowed.")
                result[self._eval(key)] = self._eval(value)
            return result
        if isinstance(node, ast.Attribute):
            return self._attribute(node)
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Subscript):
            return self._subscript(node)
        if isinstance(node, ast.UnaryOp):
            return self._unary(node)
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        raise ToolError(
            f"Line {node.lineno}: {type(node).__name__} expressions are not"
            " allowed in queries."
        )

    def _attribute(self, node: ast.Attribute) -> Any:
        value = self._eval(node.value)
        name = node.attr
        if isinstance(value, type) and issubclass(value, Model):
            managers = value._meta.managers_map
            if name in managers:
                queryset = managers[name].get_queryset()
                if self.using is not None:
                    queryset = queryset.using(self.using)
                return queryset
            raise ToolError(
                f"Line {node.lineno}: {value.__name__}.{name} is not available."
                f" Managers: {', '.join(managers)}."
            )
        if isinstance(value, QuerySet):
            if name in QUERYSET_METHODS:
                return _Method(value, name)
            raise ToolError(
                f"Line {node.lineno}: QuerySet.{name}() is not allowed. Read-only"
                f" methods available: {', '.join(sorted(QUERYSET_METHODS))}."
            )
        if isinstance(value, Combinable) and name in _EXPRESSION_METHODS:
            return _Method(value, name)
        raise ToolError(
            f"Line {node.lineno}: accessing .{name} on {_describe(value)} is not"
            " allowed."
        )

    def _call(self, node: ast.Call) -> Any:
        func = self._eval(node.func)
        args = [self._eval(arg) for arg in node.args]
        kwargs: dict[str, Any] = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                raise ToolError(f"Line {node.lineno}: ** unpacking is not allowed.")
            if keyword.arg in _FORBIDDEN_KEYWORDS or keyword.arg.startswith("_"):
                raise ToolError(
                    f"Line {node.lineno}: the {keyword.arg} argument is not allowed."
                )
            kwargs[keyword.arg] = self._eval(keyword.value)
        if isinstance(func, _Method):
            receiver = func.receiver
            if isinstance(receiver, QuerySet) and func.name in _EXECUTING_METHODS:
                self._check_query(*self._probe(receiver, func.name, args, kwargs))
            return getattr(receiver, func.name)(*args, **kwargs)
        if any(func is allowed for allowed in self.callables):
            return func(*args, **kwargs)
        if isinstance(func, type) and issubclass(func, Model):
            raise ToolError(
                f"Line {node.lineno}: calling {func.__name__}() is not allowed."
                f" Query through {func.__name__}.objects instead."
            )
        raise ToolError(
            f"Line {node.lineno}: calling {_describe(func)} is not allowed."
        )

    def _subscript(self, node: ast.Subscript) -> Any:
        value = self._eval(node.value)
        if not isinstance(value, QuerySet):
            raise ToolError(
                f"Line {node.lineno}: subscripting {_describe(value)} is not allowed."
            )
        bounds: list[int | None] = []
        if isinstance(node.slice, ast.Slice) and node.slice.step is None:
            for bound in (node.slice.lower, node.slice.upper):
                if bound is None:
                    bounds.append(None)
                elif (
                    isinstance(bound, ast.Constant)
                    and isinstance(bound.value, int)
                    and not isinstance(bound.value, bool)
                    and bound.value >= 0
                ):
                    bounds.append(bound.value)
                else:
                    break
        if len(bounds) != 2:
            raise ToolError(
                f"Line {node.lineno}: slice querysets with non-negative literal"
                " bounds, like [:10] or [10:20]. Use .first() for one row."
            )
        return value[bounds[0] : bounds[1]]

    def _unary(self, node: ast.UnaryOp) -> Any:
        operand = self._eval(node.operand)
        if isinstance(node.op, ast.USub):
            if isinstance(operand, bool) or not isinstance(
                operand, (int, float, Combinable)
            ):
                raise ToolError(
                    f"Line {node.lineno}: unary minus applies to numbers and"
                    " expressions only."
                )
            return -operand
        if isinstance(node.op, ast.Invert) and isinstance(operand, Q):
            return ~operand
        raise ToolError(
            f"Line {node.lineno}: {type(node.op).__name__} is not allowed here."
        )

    def _binary(self, node: ast.BinOp) -> Any:
        apply = _BINARY_OPERATORS.get(type(node.op))
        if apply is None:
            raise ToolError(
                f"Line {node.lineno}: the {type(node.op).__name__} operator is not"
                " allowed."
            )
        left = self._eval(node.left)
        right = self._eval(node.right)
        if not isinstance(left, _ARITHMETIC_TYPES) and not isinstance(
            right, _ARITHMETIC_TYPES
        ):
            raise ToolError(
                f"Line {node.lineno}: operators apply to expressions, such as"
                " F() and Q(), and to dates, not to plain values."
            )
        return apply(left, right)

    # Query checks

    def _probe(
        self,
        queryset: QuerySet[Any],
        name: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> tuple[Query, list[Any]]:
        """
        The query an executing method would run, for checking before it does,
        plus any expressions resolved against it. get(), latest(),
        earliest(), and aggregate() add to the query, so their arguments are
        applied as Django applies them.
        """
        if name == "get" and (args or kwargs):
            return queryset.filter(*args, **kwargs).query, []
        if name in ("latest", "earliest"):
            fields: Any = args or queryset.model._meta.get_latest_by or ()
            if isinstance(fields, str):
                fields = (fields,)
            if fields:
                return queryset.order_by(*fields).query, []
        if name == "aggregate":
            query = queryset.query.chain()
            expressions = {arg.default_alias: arg for arg in args}
            expressions.update(kwargs)
            resolved = [
                expression.resolve_expression(
                    query, allow_joins=True, reuse=None, summarize=True
                )
                for expression in expressions.values()
            ]
            return query, resolved
        return queryset.query, []

    def _check_query(
        self,
        query: Query,
        extra: Iterable[Any] = (),
        seen: set[int] | None = None,
    ) -> None:
        """
        Check every table the query reads and every column it names, after
        compiling it, since ordering and DISTINCT ON joins appear only then.
        Nested queries, in subqueries and combinators, are checked the same.
        """
        # A nested query can be reached by several paths. The ids are only
        # valid while the tree holds the queries, so seen lasts one check.
        if seen is None:
            seen = set()
        if id(query) in seen:
            return
        seen.add(id(query))
        model = query.model
        assert model is not None  # never a query without a model here
        compiler: Any = query.get_compiler(
            using=self.using or router.db_for_read(model)
        )
        try:
            _, order_by, _ = compiler.pre_sql_setup()
        except (EmptyResultSet, FullResultSet):
            # The query will not be run at all.
            return
        for alias, join in query.alias_map.items():
            if query.alias_refcount[alias]:
                self._check_table(join.table_name)
        stack: list[Any] = [
            *extra,
            query.where,
            *query.annotations.values(),
            *(expression for expression, _, _ in compiler.select),
            *(expression for expression, _ in order_by),
        ]
        if isinstance(query.group_by, tuple):
            stack.extend(query.group_by)
        while stack:
            expression = stack.pop()
            if isinstance(expression, Query):
                self._check_query(expression, seen=seen)
                continue
            if isinstance(expression, Col):
                self._check_field(expression.target)
            stack.extend(
                source
                for source in expression.get_source_expressions()
                if source is not None
            )
        for combined in query.combined_queries:
            self._check_query(combined, seen=seen)
        if query.distinct_fields:
            # DISTINCT ON joins are set up at compile time, like ordering.
            probe = query.chain()
            probe.add_distinct_fields()
            probe.clear_ordering(force=True)
            probe.add_ordering(*query.distinct_fields)
            self._check_query(probe, seen=seen)

    def _check_table(self, table_name: str) -> None:
        models = self.table_models.get(table_name, [])
        for model in models:
            if model in self.models:
                return
            # Automatic many-to-many tables come with their model...
            auto_created: Any = model._meta.auto_created
            if auto_created in self.models:
                return
            # ...and parent tables with multi-table inheritance children.
            if any(model in child._meta.get_parent_list() for child in self.models):
                return
        label = ", ".join(model._meta.label for model in models) or table_name
        raise ToolError(
            f"The query reads {label}, which is not available to query. Remove the"
            " references to it, or add .order_by() to clear a default ordering"
            " that uses it."
        )

    def _check_field(self, field: Field[Any, Any]) -> None:
        if field in self.hidden:
            raise ToolError(
                f"The field {field.model._meta.label}.{field.name} is not available."
                " Name the fields to return with .values(...) or .values_list(...)."
            )

    # Results

    def check_result(self, value: Any) -> None:
        if isinstance(value, QuerySet):
            if value._iterable_class not in _VALUES_ITERABLES:
                raise ToolError(
                    "End the query with .values(...) or .values_list(...) to"
                    " choose the fields to return."
                )
            self._check_query(value.query)
            return
        self._check_value(value)

    def _check_value(self, value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                self._check_value(item)
        elif isinstance(value, dict):
            for item in value.values():
                self._check_value(item)
        elif isinstance(value, Model):
            raise ToolError(
                "The result contains model instances. End the query with"
                " .values(...) or .values_list(...) to choose the fields to return."
            )
        elif isinstance(value, _Method):
            raise ToolError("The result is a method. Call it.")
        elif isinstance(value, (BaseManager, QuerySet, Combinable, Q)) or callable(
            value
        ):
            raise ToolError(
                f"The result is {_describe(value)}, not query results. End the"
                " query with .values(...), .values_list(...), .count(),"
                " .aggregate(...), .exists(), .first(), .last(), or .get(...)."
            )


class _QueryTool:
    def __init__(
        self,
        *,
        models: list[type[Model]],
        model_permission: Callable[[HttpRequest, type[Model]], bool],
        hidden: frozenset[Field[Any, Any]],
        using: str | None,
        timeout: float | None,
        max_rows: int,
        docstrings: bool,
    ) -> None:
        self.models = models
        self.model_permission = model_permission
        self.hidden = hidden
        self.using = using
        self.timeout = timeout
        self.max_rows = max_rows
        self.docstrings = docstrings

    def permitted_models(self, request: HttpRequest) -> list[type[Model]]:
        return [model for model in self.models if self.model_permission(request, model)]

    def description(self, instructions: str | None) -> str:
        example_model = self.models[0]
        example = (
            f"    from {example_model.__module__} import {example_model.__name__}\n"
            "    from django.db.models import Count\n"
            f"    result = {example_model.__name__}.objects.filter(...)"
            '.annotate(n=Count("pk")).values("pk", "n").order_by("-n")[:10]'
        )
        timeout = (
            f" Queries time out after {self.timeout:g} seconds."
            if self.timeout is not None
            else ""
        )
        text = (
            "Read from the database with Django ORM queries. Call with no"
            " arguments to list the apps and models available; with app to list"
            " an app's models, their fields, and the import line for each; or"
            " with query to run a query.\n\n"
            "A query is Python code: import the models and expressions it needs,"
            " then assign the query to result, ending with .values(...) or"
            " .values_list(...) naming the fields to return, or with .count(),"
            " .exists(), .aggregate(...), .first(), .last(), or .get(...)."
            " For example:\n\n"
            f"{example}\n\n"
            "Imports allowed: models, from the modules the app listings show;"
            " F, Q, Value, OuterRef, Subquery, Exists, Case, When, Window, and"
            " the aggregates, from django.db.models; database functions from"
            " django.db.models.functions; date, datetime, time, and timedelta"
            " from datetime; Decimal from decimal; UUID from uuid; and now from"
            " django.utils.timezone. Only read-only QuerySet methods are"
            " allowed. Follow relations with double underscores, as in"
            ' values("category__name"). Results are returned as Python'
            f" literals, at most {self.max_rows} rows per call. When a query has"
            " more, the result names the slice to send for the next page, like"
            f" [{self.max_rows}:{2 * self.max_rows}].{timeout}"
        )
        if instructions is not None:
            text += f"\n\n{instructions}"
        return text

    def __call__(self, request: HttpRequest, params: QueryParams) -> str:
        if params.query is not None and params.app is not None:
            raise ToolError("Pass either query or app, not both.")
        models = self.permitted_models(request)
        if params.query is not None:
            return self.query(models, params.query)
        if params.app is not None:
            return self.list_app(models, params.app)
        return self.list_apps(models)

    # Discovery

    def list_apps(self, models: list[type[Model]]) -> str:
        if not models:
            raise ToolError("No models are available to query.")
        by_app: dict[str, list[str]] = {}
        for model in models:
            by_app.setdefault(model._meta.app_label, []).append(model.__name__)
        lines = [
            "Apps and models available to query. Call again with app=<label> for"
            " each model's fields and import line, or with query=<code> to run"
            " a query.",
            "",
        ]
        for app_label in sorted(by_app):
            lines.append(f"{app_label}: {', '.join(sorted(by_app[app_label]))}")
        return "\n".join(lines)

    def list_app(self, models: list[type[Model]], app_label: str) -> str:
        app_models = sorted(
            (model for model in models if model._meta.app_label == app_label),
            key=lambda model: model.__name__,
        )
        if not app_models:
            available = ", ".join(sorted({m._meta.app_label for m in models}))
            raise ToolError(
                f"No app {app_label!r} is available to query."
                f" Available apps: {available or 'none'}."
            )
        lines = [
            f"Models in the {app_label} app, with their fields as name: type."
            " Follow relations with double underscores, as in"
            ' .values("category__name") or .filter(category__name="Toys").',
        ]
        for model in app_models:
            lines.append("")
            lines.extend(self._describe_model(model, models))
        return "\n".join(lines)

    def _describe_model(
        self, model: type[Model], available: list[type[Model]]
    ) -> list[str]:
        lines = [
            model.__name__,
            f"  from {model.__module__} import {model.__name__}",
            f"  Managers: {', '.join(model._meta.managers_map)}",
        ]
        if self.docstrings:
            docstring = _model_docstring(model)
            if docstring is not None:
                lines.append("  " + docstring.replace("\n", "\n  "))
        forward = []
        reverse = []
        for field in model._meta.get_fields():
            if field in self.hidden:
                continue
            if field.auto_created and not field.concrete:
                reverse.append(field)
            else:
                forward.append(field)
        lines.append("  Fields:")
        lines.extend(self._describe_field(field, available) for field in forward)
        if reverse:
            lines.append("  Reverse relations:")
            lines.extend(self._describe_field(field, available) for field in reverse)
        return lines

    def _describe_field(self, field: Any, available: list[type[Model]]) -> str:
        related_model = field.related_model
        if field.auto_created and not field.concrete:
            kind = f"reverse of {related_model._meta.label}.{field.field.name}"
        elif related_model is not None:
            kind = f"{type(field).__name__} to {related_model._meta.label}"
        else:
            kind = type(field).__name__
        if related_model is not None and related_model not in available:
            kind += " (not available)"
        flags = []
        if getattr(field, "primary_key", False):
            flags.append("primary key")
        elif getattr(field, "unique", False):
            flags.append("unique")
        if field.null:
            flags.append("nullable")
        choices = getattr(field, "flatchoices", None)
        if choices:
            shown = ", ".join(f"{value!r} ({label})" for value, label in choices[:20])
            if len(choices) > 20:
                shown += ", ..."
            flags.append(f"choices: {shown}")
        line = f"    {field.name}: {kind}"
        if flags:
            line += ", " + ", ".join(flags)
        help_text = str(getattr(field, "help_text", ""))
        if help_text:
            line += f" — {help_text}"
        return line

    # Queries

    def query(self, models: list[type[Model]], source: str) -> str:
        if len(source) > MAX_QUERY_LENGTH:
            raise ToolError(f"The query is longer than {MAX_QUERY_LENGTH} characters.")
        if not models:
            raise ToolError("No models are available to query.")
        table_models: dict[str, list[type[Model]]] = {}
        for model in apps.get_models(include_auto_created=True):
            table_models.setdefault(model._meta.db_table, []).append(model)
        evaluator = _Evaluator(
            models=models,
            hidden=self.hidden,
            table_models=table_models,
            using=self.using,
        )
        if self.using is not None:
            aliases = {self.using}
        else:
            aliases = {router.db_for_read(model) for model in models}
        state = _TimeoutState()
        try:
            with contextlib.ExitStack() as stack:
                if self.timeout is not None:
                    for alias in sorted(aliases):
                        stack.enter_context(
                            _timeout(connections[alias], self.timeout, state)
                        )
                value = evaluator.run(source)
                evaluator.check_result(value)
                next_page = None
                if isinstance(value, QuerySet):
                    # Fetch one row more than the limit, to learn whether
                    # the query has more, without counting them all.
                    start = value.query.low_mark
                    value = list(value[: self.max_rows + 1])
                    if len(value) > self.max_rows:
                        value = value[: self.max_rows]
                        next_page = (start + self.max_rows, start + 2 * self.max_rows)
        except ToolError:
            raise
        except DatabaseError as exc:
            if _is_timeout(exc, state):
                raise ToolError(
                    f"The query took longer than {self.timeout:g} seconds and"
                    " was cancelled. Narrow it with filters or a slice."
                ) from None
            raise ToolError(f"{type(exc).__name__}: {exc}") from None
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from None
        text = pprint.pformat(value, width=100, sort_dicts=False)
        if next_page is not None:
            text += (
                f"\n\nThe query has more rows than the {self.max_rows} shown."
                " For the next page, send it again sliced"
                f" [{next_page[0]}:{next_page[1]}], or narrow it."
            )
        return text


def add_query_tool(
    server: MCPServer,
    *,
    models: Iterable[str | type[Model]],
    name: str = "query",
    title: str | None = None,
    instructions: str | None = None,
    permission: Callable[[HttpRequest], bool] | str | None = None,
    model_permission: Callable[[HttpRequest, type[Model]], bool] | None = None,
    hidden_fields: Iterable[str] = (),
    using: str | None = None,
    timeout: float | None = 30.0,
    max_rows: int = 200,
    docstrings: bool = False,
) -> None:
    """
    Register a tool on the server that runs read-only ORM queries, written as
    Python by the calling model, against the given models.
    """
    resolved = _resolve_models(models)
    if timeout is not None and timeout <= 0:
        raise ImproperlyConfigured("timeout must be positive, or None.")
    if max_rows < 1:
        raise ImproperlyConfigured("max_rows must be at least 1.")
    if using is not None and using not in connections:
        raise ImproperlyConfigured(f"using names an unknown database: {using!r}.")
    model_permitted = _view_permission if model_permission is None else model_permission
    tool = _QueryTool(
        models=resolved,
        model_permission=model_permitted,
        hidden=_resolve_hidden_fields(resolved, hidden_fields),
        using=using,
        timeout=timeout,
        max_rows=max_rows,
        docstrings=docstrings,
    )
    if permission is None:

        def permission(request: HttpRequest) -> bool:
            return bool(tool.permitted_models(request))

    def query(request: HttpRequest, params: QueryParams) -> str:
        return tool(request, params)

    server.tool(
        name=name,
        title=title,
        description=tool.description(instructions),
        input_schema=QueryParams,
        read_only=True,
        idempotent=True,
        open_world=False,
        permission=permission,
    )(query)
