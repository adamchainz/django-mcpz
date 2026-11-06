"""
The example project schema, including the views behind the MenuItem and
OrderLine models, for the SQL tools.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

VIEWS = [
    (
        "pizzeria_report_menu_item",
        """
        SELECT id, name, price, vegetarian, available_from, available_until
        FROM pizzeria_pizza
        """,
    ),
    (
        "pizzeria_report_order_line",
        """
        SELECT
            id,
            pizza_id,
            quantity,
            total,
            placed_at,
            requests <> '' AS has_requests,
            channel
        FROM pizzeria_order
        """,
    ),
]


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Pizza",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField()),
                ("price", models.DecimalField(decimal_places=2, max_digits=5)),
                ("vegetarian", models.BooleanField(default=False)),
                ("notes", models.TextField(blank=True)),
                ("available_from", models.DateField(blank=True, null=True)),
                ("available_until", models.DateField(blank=True, null=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Order",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.PositiveSmallIntegerField()),
                ("requests", models.TextField(blank=True)),
                ("total", models.DecimalField(decimal_places=2, max_digits=7)),
                ("placed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("web", "Website"),
                            ("phone", "Telephone"),
                            ("mcp", "An AI assistant, over MCP"),
                        ],
                        default="web",
                        max_length=10,
                    ),
                ),
                (
                    "pizza",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="pizzeria.pizza"
                    ),
                ),
            ],
        ),
        *(
            migrations.RunSQL(
                f"CREATE VIEW {name} AS {query}",
                f"DROP VIEW {name}",
            )
            for name, query in VIEWS
        ),
        migrations.CreateModel(
            name="MenuItem",
            fields=[
                ("id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                (
                    "price",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Current price, in pounds.",
                        max_digits=5,
                    ),
                ),
                ("vegetarian", models.BooleanField()),
                (
                    "available_from",
                    models.DateField(
                        help_text="First day on the menu. Null for always.", null=True
                    ),
                ),
                (
                    "available_until",
                    models.DateField(
                        help_text="Last day on the menu. Null for no end date.",
                        null=True,
                    ),
                ),
            ],
            options={
                "db_table": "pizzeria_report_menu_item",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="OrderLine",
            fields=[
                ("id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("quantity", models.PositiveSmallIntegerField()),
                (
                    "total",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Price paid for the whole order, in pounds.",
                        max_digits=7,
                    ),
                ),
                (
                    "placed_at",
                    models.DateTimeField(
                        help_text="When the order was placed, in UTC."
                    ),
                ),
                (
                    "has_requests",
                    models.BooleanField(
                        help_text="Whether the customer added requests for the kitchen."
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("web", "Website"),
                            ("phone", "Telephone"),
                            ("mcp", "An AI assistant, over MCP"),
                        ],
                        max_length=10,
                    ),
                ),
            ],
            options={
                "db_table": "pizzeria_report_order_line",
                "managed": False,
            },
        ),
    ]
