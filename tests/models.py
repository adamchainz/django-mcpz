from __future__ import annotations

from django.db import models


class Category(models.Model):
    """A grouping of widgets, shown in the shop's navigation."""

    name = models.CharField(
        max_length=100, unique=True, help_text="Shown in the navigation."
    )

    class Meta:
        ordering = ["name"]


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)


class Supplier(models.Model):
    name = models.CharField(max_length=100)
    # For hidden field tests.
    secret_rate = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        ordering = ["name"]


class Widget(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    in_stock = models.BooleanField(default=True)
    colour = models.CharField(
        max_length=10,
        choices=[("red", "Red"), ("blue", "Blue")],
        default="red",
        help_text="The main colour.",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="widgets",
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True
    )
    tags = models.ManyToManyField(Tag, blank=True)

    class Meta:
        ordering = ["name"]


class PremiumWidget(Widget):
    warranty_years = models.PositiveSmallIntegerField(default=1)


class Sprocket(models.Model):
    # Default ordering that follows a relation, for join checks.
    widget = models.ForeignKey(Widget, on_delete=models.CASCADE)
    size = models.PositiveIntegerField()

    class Meta:
        ordering = ["widget__name", "size"]
