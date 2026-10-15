from __future__ import annotations

import datetime as dt

from django.db import models


class Pizza(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField()
    price = models.DecimalField(max_digits=5, decimal_places=2)
    vegetarian = models.BooleanField(default=False)
    # Freeform notes for humans, and models, to read: dietary options,
    # requests the kitchen will honour, warnings.
    notes = models.TextField(blank=True)
    # Specials and seasonal pizzas run between these dates, inclusive.
    # Null means no limit at that end.
    available_from = models.DateField(null=True, blank=True)
    available_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def available_on(self, date: dt.date) -> bool:
        return (self.available_from is None or self.available_from <= date) and (
            self.available_until is None or date <= self.available_until
        )


class Order(models.Model):
    pizza = models.ForeignKey(Pizza, on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField()
    requests = models.TextField(blank=True)
    total = models.DecimalField(max_digits=7, decimal_places=2)
    placed_at = models.DateTimeField(auto_now_add=True)
    channel = models.CharField(
        max_length=10,
        choices=[
            ("web", "Website"),
            ("phone", "Telephone"),
            ("mcp", "An AI assistant, over MCP"),
        ],
        default="web",
    )

    def __str__(self) -> str:
        return f"{self.quantity}× {self.pizza.name}"


# Read-only views for the SQL tools, created in migration 0004. The models
# are unmanaged, so Django never tries to create or alter them, but they
# describe the columns to the model, with help_text and choices.


class MenuItem(models.Model):
    """
    Every pizza MCPizza has sold, including specials no longer on the menu.
    """

    id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=100)
    price = models.DecimalField(
        max_digits=5, decimal_places=2, help_text="Current price, in pounds."
    )
    vegetarian = models.BooleanField()
    available_from = models.DateField(
        null=True, help_text="First day on the menu. Null for always."
    )
    available_until = models.DateField(
        null=True, help_text="Last day on the menu. Null for no end date."
    )

    class Meta:
        managed = False
        db_table = "pizzeria_report_menu_item"

    def __str__(self) -> str:
        return self.name


class OrderLine(models.Model):
    """
    One row per order. The customer’s free-text requests are not exposed,
    only whether there were any.
    """

    id = models.BigIntegerField(primary_key=True)
    pizza = models.ForeignKey(MenuItem, on_delete=models.DO_NOTHING)
    quantity = models.PositiveSmallIntegerField()
    total = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        help_text="Price paid for the whole order, in pounds.",
    )
    placed_at = models.DateTimeField(help_text="When the order was placed, in UTC.")
    has_requests = models.BooleanField(
        help_text="Whether the customer added requests for the kitchen."
    )
    channel = models.CharField(
        max_length=10, choices=Order._meta.get_field("channel").choices
    )

    class Meta:
        managed = False
        db_table = "pizzeria_report_order_line"

    def __str__(self) -> str:
        return f"Order {self.id}"
