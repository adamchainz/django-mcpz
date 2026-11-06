"""
Seed the data:

* The menu. Specials are dated relative to the day this migration runs, so
  "what can I order tomorrow?" always has an interesting answer.
* Three months of orders, so the SQL tools have something to report on.
  Orders are random, but seeded, so every install sees the same trends:
  weekends are busier, and phone orders are dwindling as AI assistants take
  over.
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def seed_menu(apps, schema_editor):
    Pizza = apps.get_model("pizzeria", "Pizza")
    today = dt.date.today()
    day = dt.timedelta(days=1)

    pizzas = [
        {
            "name": "Margherita of Theseus",
            "description": (
                "Tomato, mozzarella, basil. Every ingredient has been replaced"
                " at least once since 1997. Still a Margherita? Discuss."
            ),
            "price": "9.50",
            "vegetarian": True,
            "notes": "Vegan cheese available on request.",
        },
        {
            "name": "Null Pointer",
            "description": "No toppings. No sauce. A base. You asked for it.",
            "price": "6.00",
            "vegetarian": True,
            "notes": (
                "Vegan by definition. Gluten-free base available, ask when"
                " ordering. Adding sauce upgrades it to a Segfault."
            ),
        },
        {
            "name": "The Hawaiian Standoff",
            "description": "Ham and pineapple, served with a side of debate.",
            "price": "11.00",
            "vegetarian": False,
            "notes": "Pineapple can be removed on request, but we will remember.",
        },
        {
            "name": "Pepperoni Overflow",
            "description": (
                "Pepperoni in quantities that exceed the boundaries of the pizza."
            ),
            "price": "12.50",
            "vegetarian": False,
            "notes": "Comes in a bigger box than you would expect.",
        },
        {
            "name": "Four Cheese, Five If You’re Nice",
            "description": "Mozzarella, gorgonzola, parmesan, taleggio.",
            "price": "12.00",
            "vegetarian": True,
            "notes": "Fifth cheese at the kitchen’s discretion. Be nice.",
        },
        {
            "name": "The Off-By-One",
            "description": "Mushrooms, olives, red onion. Cut into eleven slices. Or nine.",
            "price": "10.50",
            "vegetarian": True,
            "notes": "Never ten slices. Vegan cheese available on request.",
        },
        {
            "name": "Nduja Believe It",
            "description": "Nduja, chilli, honey. Extremely spicy.",
            "price": "13.00",
            "vegetarian": False,
            "notes": (
                "We are not joking about the spice. Contains honey, so not"
                " vegan even with vegan cheese."
            ),
        },
        {
            "name": "Anchovy Alarm",
            "description": "Anchovies, capers, garlic, oregano. Bracing.",
            "price": "11.50",
            "vegetarian": False,
            "notes": "Contains fish. This is the whole point of it.",
        },
        {
            "name": "Truffle Shuffle",
            "description": "Wild mushrooms, truffle oil, ricotta.",
            "price": "16.00",
            "vegetarian": True,
            "notes": (
                "Truffle oil, not truffles. We are a pizza place, not a hedge"
                " fund. Contains dairy, no vegan option."
            ),
        },
        {
            "name": "Garlic Bread (Technically a Pizza)",
            "description": "Garlic butter, parsley. Legally a pizza in three jurisdictions.",
            "price": "5.50",
            "vegetarian": True,
            "notes": "Gluten-free base available, ask when ordering.",
        },
        # Specials, dated relative to today.
        {
            "name": "Pesto Late Than Never",
            "description": "Basil pesto, cherry tomatoes, pine nuts. The special that starts tomorrow.",
            "price": "12.00",
            "vegetarian": True,
            "notes": "Contains pine nuts. Vegan cheese available on request.",
            "available_from": today + day,
            "available_until": today + 14 * day,
        },
        {
            "name": "Last Day of Summer Melt",
            "description": "Grilled courgette, corn, lemon ricotta. Today only, then it is gone.",
            "price": "11.00",
            "vegetarian": True,
            "notes": "Contains dairy, no vegan option.",
            "available_from": today - 30 * day,
            "available_until": today,
        },
        {
            "name": "The Discontinued",
            "description": "We do not talk about The Discontinued.",
            "price": "9.00",
            "vegetarian": False,
            "notes": "Withdrawn after The Incident.",
            "available_until": today - day,
        },
        {
            "name": "The Leap Year",
            "description": "Every topping we have, once every 1461 days.",
            "price": "29.02",
            "vegetarian": False,
            "notes": "Worth the wait. Probably.",
            "available_from": dt.date(2028, 2, 29),
            "available_until": dt.date(2028, 2, 29),
        },
    ]
    for pizza in pizzas:
        pizza["price"] = Decimal(pizza["price"])
    Pizza.objects.bulk_create(Pizza(**pizza) for pizza in pizzas)


REQUESTS = [
    "Vegan cheese please.",
    "Gluten-free base.",
    "Extra crispy.",
    "Cut into squares.",
    "No basil.",
]


def seed_orders(apps, schema_editor):
    Pizza = apps.get_model("pizzeria", "Pizza")
    Order = apps.get_model("pizzeria", "Order")
    # This historical model is private to the migration, so it is safe to
    # stop auto_now_add overwriting the backdated times.
    Order._meta.get_field("placed_at").auto_now_add = False

    rng = random.Random(1997)
    pizzas = list(Pizza.objects.order_by("id"))
    # Some pizzas are more popular than others.
    weights = [rng.uniform(0.5, 3.0) for _ in pizzas]
    today = timezone.localdate()
    orders = []
    for days_ago in range(90, 0, -1):
        day = today - dt.timedelta(days=days_ago)
        available = [
            (pizza, weight)
            for pizza, weight in zip(pizzas, weights, strict=True)
            if (pizza.available_from is None or pizza.available_from <= day)
            and (pizza.available_until is None or day <= pizza.available_until)
        ]
        busy = 1.6 if day.weekday() >= 4 else 1.0
        progress = (90 - days_ago) / 90
        for _ in range(int(rng.randint(8, 16) * busy)):
            pizza = rng.choices([p for p, _ in available], [w for _, w in available])[0]
            quantity = rng.choices([1, 2, 3, 4, 6], [50, 25, 12, 8, 5])[0]
            channel = rng.choices(
                ["web", "phone", "mcp"],
                [50, 40 - 30 * progress, 10 + 30 * progress],
            )[0]
            minutes = rng.randint(11 * 60 + 30, 22 * 60)
            placed_at = dt.datetime.combine(
                day, dt.time(minutes // 60, minutes % 60), dt.UTC
            )
            orders.append(
                Order(
                    pizza=pizza,
                    quantity=quantity,
                    total=pizza.price * quantity,
                    requests=rng.choice(REQUESTS) if rng.random() < 0.2 else "",
                    placed_at=placed_at,
                    channel=channel,
                )
            )
    Order.objects.bulk_create(orders)


class Migration(migrations.Migration):
    dependencies = [("pizzeria", "0001_initial")]

    operations = [
        migrations.RunPython(seed_menu, migrations.RunPython.noop),
        migrations.RunPython(seed_orders, migrations.RunPython.noop),
    ]
