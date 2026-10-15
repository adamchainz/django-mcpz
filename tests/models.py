from __future__ import annotations

from django.db import models


class Widget(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    in_stock = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]


class WidgetView(models.Model):
    """
    Widgets for sale, without their stock levels.
    """

    id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=100)
    price = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="In euros, with VAT."
    )

    class Meta:
        managed = False
        db_table = "tests_widget_view"


class Part(models.Model):
    # Described, never queried, so it has no table.
    widget = models.ForeignKey(WidgetView, on_delete=models.DO_NOTHING)
    sku = models.CharField(
        "stock keeping unit", max_length=20, db_comment="Unique per supplier."
    )
    size = models.CharField(
        max_length=1, choices=[("S", "Small"), ("L", "Large")], null=True
    )
    supplier = models.ForeignKey(Widget, on_delete=models.DO_NOTHING)
    rank = models.IntegerField(choices=[(1, "First"), (2, "Second")])

    class Meta:
        managed = False
        db_table_comment = "Parts that make up\nwidgets."


class Undocumented(models.Model):
    class Meta:
        managed = False


class NoColumnField(models.IntegerField):  # type: ignore[type-arg]
    # Like fields such as GenericRelation, which have no column.
    def db_type(self, connection):
        return None


class Reserved(models.Model):
    # Described, never queried, with names that need quoting.
    user = models.IntegerField(db_column="User")
    label = models.CharField(max_length=10, verbose_name="")
    nothing = NoColumnField()

    class Meta:
        managed = False
        db_table = "order"


class Pair(models.Model):
    pk = models.CompositePrimaryKey("left_id", "right_id")
    left_id = models.IntegerField()
    right_id = models.IntegerField()

    class Meta:
        managed = False


class Parent(models.Model):
    name = models.CharField(max_length=10)

    class Meta:
        managed = False


class Child(Parent):
    age = models.IntegerField()

    class Meta:
        managed = False
