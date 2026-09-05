from __future__ import annotations

from django.db import models


class Widget(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    in_stock = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
