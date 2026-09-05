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

    def __str__(self) -> str:
        return f"{self.quantity}× {self.pizza.name}"
