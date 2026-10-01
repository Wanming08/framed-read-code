"""Execute original V2__add_media_content_hash.sql.

Revision ID: 0002_original_v2
Revises: 0001_original_v1
"""

from alembic import op

from app.db.migration_sql import execute_migration

revision = "0002_original_v2"
down_revision = "0001_original_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_migration(op.get_bind(), 2)


def downgrade() -> None:
    raise RuntimeError("the original Flyway schema has no non-destructive downgrade")
