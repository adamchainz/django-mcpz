"""
Seed the menu. Specials are dated relative to the day this migration runs,
so "what can I order tomorrow?" always has an interesting answer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import migrations


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


class Migration(migrations.Migration):
    dependencies = [("pizzeria", "0001_initial")]

    operations = [migrations.RunPython(seed_menu, migrations.RunPython.noop)]
