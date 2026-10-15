"""
Create the views behind the MenuItem and OrderLine models, for the SQL
tools. On PostgreSQL, also create the mcpizza_reader role that the tools
connect as, allowed to SELECT from those views and nothing else.
"""

from __future__ import annotations

from django.db import migrations

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

# A local demo password. For a real deployment, create the role outside of
# migrations and keep its password with your other secrets.
CREATE_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcpizza_reader') THEN
        CREATE ROLE mcpizza_reader LOGIN PASSWORD 'mcpizza';
    END IF;
END
$$;
"""


def create_role(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    quote = schema_editor.quote_name
    database = quote(schema_editor.connection.settings_dict["NAME"])
    schema_editor.execute(CREATE_ROLE)
    schema_editor.execute(f"GRANT CONNECT ON DATABASE {database} TO mcpizza_reader")
    schema_editor.execute("GRANT USAGE ON SCHEMA public TO mcpizza_reader")
    for name, _ in VIEWS:
        schema_editor.execute(f"GRANT SELECT ON {quote(name)} TO mcpizza_reader")


def drop_role(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    database = schema_editor.quote_name(schema_editor.connection.settings_dict["NAME"])
    schema_editor.execute("REVOKE USAGE ON SCHEMA public FROM mcpizza_reader")
    schema_editor.execute(f"REVOKE CONNECT ON DATABASE {database} FROM mcpizza_reader")


class Migration(migrations.Migration):
    dependencies = [("pizzeria", "0004_order_history")]

    operations = [
        *(
            migrations.RunSQL(
                f"CREATE VIEW {name} AS {query}",
                f"DROP VIEW {name}",
            )
            for name, query in VIEWS
        ),
        migrations.RunPython(create_role, drop_role),
    ]
