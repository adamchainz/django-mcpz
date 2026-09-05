from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

import msgspec
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest, HttpResponse

from django_mcpz.server import Icon, MCPServer, ToolError, public
from django_mcpz.tokens.auth import token_auth

server = MCPServer(
    name="example-server",
    version="1.2.3",
    title="Example Server",
    instructions="Example MCP server used in the django-mcpz test suite.",
    auth=public,
)


@server.tool(
    description="Add two integers.",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"sum": {"type": "integer"}},
        "required": ["sum"],
    },
)
def add(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, int]:
    return {"sum": arguments["a"] + arguments["b"]}


@server.tool(
    name="greet",
    title="Greeter",
    description="Greet someone by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    },
    read_only=True,
)
def greet_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return f"Hello, {arguments.get('name', 'world')}!"


@server.tool(
    description="Report an in-band tool execution error.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unavailable(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ToolError("The flux capacitor is offline. Try the DeLorean instead.")


@server.tool(
    description="Crash with an unexpected exception.",
    input_schema={"type": "object", "additionalProperties": False},
    destructive=True,
    open_world=False,
)
def crash(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ValueError("secret internal details")


@server.tool(
    description="Do nothing.",
    input_schema={"type": "object", "additionalProperties": False},
)
def noop(request: HttpRequest, arguments: dict[str, Any]) -> None:
    return None


@server.tool(
    description="Return something JSON cannot represent.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unencodable(request: HttpRequest, arguments: dict[str, Any]) -> object:
    return object()


@server.tool(
    description="Echo the region.",
    input_schema={
        "type": "object",
        "properties": {
            "region": {"type": "string"},
            "shard": {"type": "integer"},
            "fast": {"type": "boolean"},
            "config": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string"},
                },
            },
        },
        "additionalProperties": False,
    },
    icons=[{"src": "https://example.com/regional.png", "mimeType": "image/png"}],
)
def regional(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"region": arguments.get("region")}


def bearer_auth(request: HttpRequest) -> HttpResponse | None:
    if request.headers.get("Authorization") != "Bearer test-token":
        return HttpResponse(status=HTTPStatus.UNAUTHORIZED)
    return None


secure_server = MCPServer(
    name="secure-server",
    version="1.0.0",
    auth=bearer_auth,
)


@secure_server.tool(
    description="Return the secret word.",
    input_schema={"type": "object", "additionalProperties": False},
)
def secret_word(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "xyzzy"


# Typed schemas: msgspec Structs generate the JSON Schemas and validate
# arguments before the tool runs.


class MultiplyParams(msgspec.Struct):
    a: Annotated[int, msgspec.Meta(description="The first factor.")]
    b: int = 2


class MultiplyResult(msgspec.Struct):
    product: int


@server.tool(
    description="Multiply two integers.",
    input_schema=MultiplyParams,
    output_schema=MultiplyResult,
)
def multiply(request: HttpRequest, params: MultiplyParams) -> MultiplyResult:
    return MultiplyResult(product=params.a * params.b)


class AddTypedParams(msgspec.Struct, forbid_unknown_fields=True):
    a: int
    b: int


@server.tool(
    description="Add two integers, with typed parameters.",
    input_schema=AddTypedParams,
)
def add_typed(request: HttpRequest, params: AddTypedParams) -> dict[str, int]:
    return {"sum": params.a + params.b}


class Point(msgspec.Struct):
    x: int
    y: int


class SegmentParams(msgspec.Struct):
    start: Point
    end: Point
    label: str = ""


@server.tool(
    description="Measure the Manhattan length of a line segment.",
    input_schema=SegmentParams,
)
def segment_length(request: HttpRequest, params: SegmentParams) -> dict[str, Any]:
    length = abs(params.end.x - params.start.x) + abs(params.end.y - params.start.y)
    return {"label": params.label, "length": length}


class ShoutParams(msgspec.Struct):
    message: Annotated[str, msgspec.Meta(description="What to shout.")]
    times: int = 1


class ShoutResult(msgspec.Struct):
    text: str


@server.tool(
    description="Shout a message.",
    read_only=True,
    idempotent=True,
    icons=[
        Icon(static="diner/shout.png", sizes=("48x48",)),
        Icon(static="diner/shout-dark.svg", mime_type="image/svg+xml", theme="dark"),
        {"src": "data:image/png;base64,AAAA", "mimeType": "image/png"},
    ],
)
def shout(request: HttpRequest, params: ShoutParams) -> ShoutResult:
    return ShoutResult(text=" ".join([params.message.upper() + "!"] * params.times))


@server.tool(description="Do nothing, with a generated empty schema.")
def sig_noop(request: HttpRequest) -> None:
    return None


# Speaks only 2026-07-28: clients on earlier revisions are rejected.
strict_server = MCPServer(
    name="strict-server",
    version="1.0.0",
    auth=public,
    minimum_protocol_version="2026-07-28",
)


@strict_server.tool(description="Do nothing, strictly.")
def strict_noop(request: HttpRequest) -> None:
    return None


# Per-tool permissions. The auth callable identifies the user from a header,
# standing in for a real token lookup.


def header_user_auth(request: HttpRequest) -> HttpResponse | None:
    username = request.headers.get("X-User")
    if username is None:
        request.user = AnonymousUser()
    else:
        request.user = User.objects.get(username=username)
    return None


perms_server = MCPServer(
    name="perms-server",
    version="1.0.0",
    auth=header_user_auth,
)


@perms_server.tool(
    description="Available to everyone.",
    input_schema={"type": "object", "additionalProperties": False},
)
def public_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "public"


@perms_server.tool(
    description="Available to staff.",
    input_schema={"type": "object", "additionalProperties": False},
    permission=lambda request: request.user.is_staff,
)
def staff_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "staff"


@perms_server.tool(
    description="Available to users with the tests.view_widget permission.",
    input_schema={"type": "object", "additionalProperties": False},
    permission="tests.view_widget",
)
def widget_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "widgets"


# Authenticated with the django_mcpz.tokens app.
tokens_server = MCPServer(name="tokens-server", version="1.0.0", auth=token_auth)


@tokens_server.tool(description="Who is calling.", read_only=True)
def whoami(request: HttpRequest) -> dict[str, Any]:
    return {
        "token": request.mcp_token.name,  # type: ignore[attr-defined]
        "user": request.user.get_username(),
    }


@tokens_server.tool(
    description="Available to users with the tests.view_widget permission.",
    permission="tests.view_widget",
)
def token_widget_tool(request: HttpRequest) -> str:
    return "widgets"
