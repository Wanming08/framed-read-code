"""Execute original V3__add_failed_task_mode.sql.

Revision ID: 0003_original_v3
Revises: 0002_original_v2
"""

from alembic import op

from app.db.migration_sql import execute_migration

revision = "0003_original_v3"
down_revision = "0002_original_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_migration(op.get_bind(), 3)


def downgrade() -> None:
    raise RuntimeError("the original Flyway schema has no non-destructive downgrade")
