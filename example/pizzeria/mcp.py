from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import msgspec
from django.http import HttpRequest

from django_mcpz.server import MCPServer, ToolError, public
from pizzeria.models import Order, Pizza

server = MCPServer(
    name="mcpizza",
    version="1.0.0",
    title="MCPizza",
    instructions=(
        "MCPizza sells pizzas. Call current_date to learn today's date, then"
        " search_menu with on_date to see what is on the menu for a given"
        " day. Read each pizza's notes for dietary options and requests the"
        " kitchen will honour. Order with place_order, using exact pizza"
        " names, and put any requests in the requests field."
    ),
    # Served on localhost only, so this example skips authentication. See
    # the docs for adding a bearer token or other auth.
    auth=public,
)


@server.tool(
    description="Today's date and weekday, for working out what is on the menu.",
    read_only=True,
)
def current_date(request: HttpRequest) -> dict[str, str]:
    today = dt.date.today()
    return {"date": today.isoformat(), "weekday": today.strftime("%A")}


class SearchMenuParams(msgspec.Struct):
    query: Annotated[
        str | None,
        msgspec.Meta(description="Case-insensitive substring match on pizza names."),
    ] = None
    # The constraint sits inside the optional, since msgspec only allows ge
    # on a numeric type, not on a union with None.
    max_price: (
        Annotated[
            float,
            msgspec.Meta(description="Only pizzas costing at most this much.", ge=0.0),
        ]
        | None
    ) = None
    on_date: Annotated[
        dt.date | None,
        msgspec.Meta(
            description="The date to show the menu for, as YYYY-MM-DD. Defaults to today."
        ),
    ] = None


def _pizza_json(pizza: Pizza) -> dict[str, Any]:
    return {
        "name": pizza.name,
        "description": pizza.description,
        "price": float(pizza.price),
        "vegetarian": pizza.vegetarian,
        "notes": pizza.notes,
        "available_from": (
            pizza.available_from.isoformat() if pizza.available_from else None
        ),
        "available_until": (
            pizza.available_until.isoformat() if pizza.available_until else None
        ),
    }


@server.tool(
    description=(
        "The pizzas on the menu for a date, with prices and notes. Notes"
        " describe dietary options, requests the kitchen will honour, and"
        " warnings, so read them rather than guessing."
    ),
    read_only=True,
)
def search_menu(request: HttpRequest, params: SearchMenuParams) -> dict[str, Any]:
    on_date = params.on_date or dt.date.today()
    pizzas = Pizza.objects.all()
    if params.query is not None:
        pizzas = pizzas.filter(name__icontains=params.query)
    if params.max_price is not None:
        pizzas = pizzas.filter(price__lte=params.max_price)
    return {
        "date": on_date.isoformat(),
        "pizzas": [_pizza_json(p) for p in pizzas if p.available_on(on_date)],
    }


class PlaceOrderParams(msgspec.Struct):
    pizza: Annotated[
        str,
        msgspec.Meta(description="Exact pizza name, as returned by search_menu."),
    ]
    quantity: Annotated[int, msgspec.Meta(ge=1, le=20)] = 1
    requests: Annotated[
        str,
        msgspec.Meta(
            description=(
                "Requests for the kitchen, such as options mentioned in the"
                " pizza's notes."
            )
        ),
    ] = ""


@server.tool(description="Order a pizza from today's menu.")
def place_order(request: HttpRequest, params: PlaceOrderParams) -> dict[str, Any]:
    try:
        pizza = Pizza.objects.get(name=params.pizza)
    except Pizza.DoesNotExist:
        raise ToolError(
            f"No pizza named {params.pizza!r}. Use search_menu to find exact names."
        ) from None
    if not pizza.available_on(dt.date.today()):
        raise ToolError(f"{pizza.name} is not on today's menu.")
    order = Order.objects.create(
        pizza=pizza,
        quantity=params.quantity,
        requests=params.requests,
        total=pizza.price * params.quantity,
    )
    return {
        "order_id": order.id,
        "pizza": pizza.name,
        "quantity": order.quantity,
        "requests": order.requests,
        "total": float(order.total),
    }
