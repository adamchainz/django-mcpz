"""
Seed three months of orders, so the SQL tools have something to report on.
Orders are random, but seeded, so every install sees the same trends:
weekends are busier, and phone orders are dwindling as AI assistants take
over.
"""

from __future__ import annotations

import datetime as dt
import random

from django.db import migrations
from django.utils import timezone

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
    dependencies = [("pizzeria", "0003_order_channel")]

    operations = [migrations.RunPython(seed_orders, migrations.RunPython.noop)]
