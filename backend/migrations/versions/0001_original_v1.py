"""Execute original V1__create_core_tables.sql without translation.

Revision ID: 0001_original_v1
Revises:
"""

from alembic import op

from app.db.migration_sql import assert_empty_business_schema, execute_migration

revision = "0001_original_v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "mysql":
        raise RuntimeError("original V1 SQL requires MySQL")
    assert_empty_business_schema(connection)
    execute_migration(connection, 1)


def downgrade() -> None:
    raise RuntimeError("the original Flyway schema has no non-destructive downgrade")
