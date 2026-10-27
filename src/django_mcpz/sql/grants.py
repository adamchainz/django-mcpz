"""
Maintaining the grants of the read-only roles defined with SQLReader, after
every migrate.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.utils.module_loading import autodiscover_modules

from django_mcpz.sql.readers import SQLReader, _readers, quote_name


def sync_readers(
    using: str, verbosity: int = 1, stdout: TextIO | None = None, **kwargs: Any
) -> None:
    """
    The post_migrate handler, syncing the grants of the readers whose
    grantor is the database alias just migrated.
    """
    # Readers are defined alongside the tools that use them, in apps' mcp
    # modules, so import those first, as the admin does for admin modules.
    autodiscover_modules("mcp")
    for reader in _readers.values():
        # SQLite readers have no role, being checked as each query runs.
        if reader.grantor == using and connections[reader.using].vendor == "postgresql":
            missing = sync_grants(reader)
            if missing and verbosity >= 1:
                (stdout or sys.stdout).write(
                    f"  Granted {reader.using!r} no access to these, missing"
                    f" from the database: {', '.join(missing)}.\n"
                )


def sync_grants(reader: SQLReader) -> list[str]:
    """
    Make the reader's role able to SELECT exactly its tables' columns, and
    nothing else.

    This runs on the reader's grantor connection. It creates the role if it's
    missing, with the reader alias's password, or else sets that password,
    if the grantor may and the alias has one, grants it CONNECT on the
    database and USAGE on the tables' schemas, revokes everything it was
    granted on tables and columns, then grants SELECT on the columns. It all
    happens in one transaction, so the role never sees a half-way state.
    """
    from psycopg import sql as pg_sql

    using = reader.grantor
    connection = connections[using]
    if connection.vendor != "postgresql":
        raise ImproperlyConfigured(
            f"The SQL tools need a PostgreSQL database, but {using!r} is"
            f" {connection.vendor}."
        )
    reader_settings = connections[reader.using].settings_dict
    role = reader_settings["USER"]
    if not role:
        raise ImproperlyConfigured(
            f"The {reader.using!r} database alias needs to set USER, the role"
            " it logs in as."
        )
    if role == connection.settings_dict["USER"]:
        raise ImproperlyConfigured(
            f"The {reader.using!r} and {using!r} database aliases log in as the"
            f" same role, {role!r}, so it can't be limited to reading tables."
        )
    with transaction.atomic(using=using):
        connection.ensure_connection()
        pg_connection = connection.connection
        with pg_connection.cursor() as cursor:
            role_id = pg_sql.Identifier(role)
            password = pg_sql.Literal(reader_settings["PASSWORD"] or None)
            cursor.execute(_ROLE_SQL, [role])
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    pg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        role_id, password
                    )
                )
            elif row[0] and reader_settings["PASSWORD"]:
                # Keep the password matching the setting, as it's rotated.
                cursor.execute(
                    pg_sql.SQL("ALTER ROLE {} PASSWORD {}").format(role_id, password)
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
            missing: list[str] = []
            for table in reader.tables:
                cursor.execute(_TABLE_SQL, [quote_name(table.name)])
                row = cursor.fetchone()
                if row is None:
                    missing.append(table.name)
                    continue
                relation, schema, existing = row
                columns = [column for column in table.columns if column in existing]
                missing.extend(
                    f"{table.name}.{column}"
                    for column in table.columns
                    if column not in existing
                )
                if not columns:
                    continue
                cursor.execute(
                    pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        pg_sql.Identifier(schema), role_id
                    )
                )
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(
                        pg_sql.SQL(", ").join(
                            [pg_sql.Identifier(column) for column in columns]
                        ),
                        pg_sql.SQL(relation),
                        role_id,
                    )
                )
    return missing


# Whether the grantor may change the role's password, if the role exists: a
# superuser, or one with CREATEROLE, which from PostgreSQL 16 also needs the
# ADMIN option on the role, as it has on roles it created.
_ROLE_SQL = """
SELECT
    grantor.rolsuper OR grantor.rolcreaterole AND (
        current_setting('server_version_num')::int < 160000
        OR pg_has_role(grantor.oid, role.oid, 'MEMBER WITH ADMIN OPTION')
    )
FROM pg_roles role, pg_roles grantor
WHERE role.rolname = %s AND grantor.rolname = current_user
"""


# A table or view, by name, with its schema and current columns.
_TABLE_SQL = """
SELECT
    c.oid::regclass::text,
    n.nspname,
    ARRAY(
        SELECT attname::text
        FROM pg_attribute
        WHERE attrelid = c.oid AND attnum > 0 AND NOT attisdropped
    )
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(%s)
"""


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
