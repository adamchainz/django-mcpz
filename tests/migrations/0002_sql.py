from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

import tests.models


class Migration(migrations.Migration):
    dependencies = [
        ("tests", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Part",
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
                (
                    "sku",
                    models.CharField(
                        db_comment="Unique per supplier.",
                        max_length=20,
                        verbose_name="stock keeping unit",
                    ),
                ),
                (
                    "size",
                    models.CharField(
                        choices=[("S", "Small"), ("L", "Large")],
                        max_length=1,
                        null=True,
                    ),
                ),
                ("rank", models.IntegerField(choices=[(1, "First"), (2, "Second")])),
            ],
            options={
                "db_table_comment": "Parts that make up\nwidgets.",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Undocumented",
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
            ],
            options={
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="WidgetView",
            fields=[
                ("id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                (
                    "price",
                    models.DecimalField(
                        decimal_places=2, help_text="In euros, with VAT.", max_digits=6
                    ),
                ),
            ],
            options={
                "db_table": "tests_widget_view",
                "managed": False,
            },
        ),
        migrations.RunSQL(
            "CREATE VIEW tests_widget_view AS SELECT id, name, price FROM tests_widget",
            "DROP VIEW tests_widget_view",
        ),
        migrations.CreateModel(
            name="Parent",
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
                ("name", models.CharField(max_length=10)),
            ],
            options={
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Pair",
            fields=[
                (
                    "pk",
                    models.CompositePrimaryKey(
                        "left_id",
                        "right_id",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("left_id", models.IntegerField()),
                ("right_id", models.IntegerField()),
            ],
            options={
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Reserved",
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
                ("user", models.IntegerField(db_column="User")),
                ("label", models.CharField(max_length=10, verbose_name="")),
                ("nothing", tests.models.NoColumnField()),
            ],
            options={
                "db_table": "order",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Child",
            fields=[
                (
                    "parent_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="tests.parent",
                    ),
                ),
                ("age", models.IntegerField()),
            ],
            options={
                "managed": False,
            },
            bases=("tests.parent",),
        ),
        migrations.CreateModel(
            name="WidgetProxy",
            fields=[],
            options={
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("tests.widget",),
        ),
        migrations.CreateModel(
            name="WidgetLater",
            fields=[
                ("id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                ("colour", models.CharField(max_length=20)),
            ],
            options={
                "db_table": "tests_widget",
                "managed": False,
            },
        ),
    ]
