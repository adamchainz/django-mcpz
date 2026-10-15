"""
Maintaining the grants of the read-only roles in MCPZ_SQL_READERS, after
every migrate.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from psycopg import sql as pg_sql

from django_mcpz.sql.conf import Reader, get_readers, quote_name


def sync_readers(using: str, **kwargs: Any) -> None:
    """
    The post_migrate handler, syncing the grants of the readers maintained
    by the database alias just migrated.
    """
    for reader in get_readers():
        if reader.using == using:
            sync_grants(reader)


def sync_grants(reader: Reader) -> None:
    """
    Make the reader's role able to SELECT exactly its tables' columns, and
    nothing else.

    This runs on the reader's using connection. It creates the role if it's
    missing, with the reader alias's password, grants it CONNECT on the
    database and USAGE on the tables' schemas, revokes everything it was
    granted on tables and columns, then grants SELECT on the columns. It all
    happens in one transaction, so the role never sees a half-way state.
    """
    using = reader.using
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise ImproperlyConfigured(
            f"The SQL tools need a PostgreSQL database, but {using!r} is"
            f" {connection.vendor}."
        )
    reader_settings = connections[reader.alias].settings_dict
    role = reader_settings["USER"]
    if not role:
        raise ImproperlyConfigured(
            f"The {reader.alias!r} database alias needs to set USER, the role"
            " it logs in as."
        )
    if role == connection.settings_dict["USER"]:
        raise ImproperlyConfigured(
            f"The {reader.alias!r} and {using!r} database aliases log in as the"
            f" same role, {role!r}, so it can't be limited to reading tables."
        )
    with transaction.atomic(using=using):
        connection.ensure_connection()
        pg_connection = connection.connection
        with pg_connection.cursor() as cursor:
            role_id = pg_sql.Identifier(role)
            cursor.execute("SELECT FROM pg_roles WHERE rolname = %s", [role])
            if cursor.fetchone() is None:
                cursor.execute(
                    pg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        role_id, pg_sql.Literal(reader_settings["PASSWORD"] or None)
                    )
                )
            cursor.execute(
                pg_sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    pg_sql.Identifier(pg_connection.info.dbname), role_id
                )
            )
            # Everything the role has been granted, on tables or columns.
            # Revoking on a table revokes on its columns too.
            cursor.execute(_GRANTED_TABLES_SQL, {"role": role})
            for (relation,) in cursor.fetchall():
                cursor.execute(
                    pg_sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(
                        pg_sql.SQL(relation), role_id
                    )
                )
            for table in reader.tables:
                cursor.execute(
                    "SELECT to_regclass(%s)::text, n.nspname FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.oid = to_regclass(%s)",
                    [quote_name(table.name), quote_name(table.name)],
                )
                row = cursor.fetchone()
                if row is None:
                    raise ImproperlyConfigured(
                        f"Can't find the table {table.name!r} for the"
                        f" {reader.alias!r} reader. Create it, or its view, in"
                        " a migration."
                    )
                relation, schema = row
                cursor.execute(
                    pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        pg_sql.Identifier(schema), role_id
                    )
                )
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(
                        pg_sql.SQL(", ").join(
                            [pg_sql.Identifier(column) for column in table.columns]
                        ),
                        pg_sql.SQL(relation),
                        role_id,
                    )
                )


# Tables, views, and so on, where the role holds privileges on the relation
# or on any of its columns, granted explicitly rather than through PUBLIC.
_GRANTED_TABLES_SQL = """
SELECT c.oid::regclass::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
AND (
    EXISTS (
        SELECT FROM aclexplode(c.relacl) a
        WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %(role)s)
    )
    OR EXISTS (
        SELECT FROM pg_attribute att, aclexplode(att.attacl) a
        WHERE att.attrelid = c.oid
        AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %(role)s)
    )
)
ORDER BY 1
"""
