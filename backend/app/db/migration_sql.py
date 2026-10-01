"""Execute pinned Flyway SQL through one Alembic connection per revision."""

from __future__ import annotations

from pathlib import Path
from sqlalchemy import inspect

# Bundle the unchanged upstream baseline with the Python deployment resources.
SOURCE_DIR = Path(__file__).resolve().parents[2] / "migrations/sql"
SOURCE_NAMES = {
    1: "V1__create_core_tables.sql",
    2: "V2__add_media_content_hash.sql",
    3: "V3__add_failed_task_mode.sql",
}
BUSINESS_TABLES = frozenset({"users", "media_files", "agent_checkpoints", "failed_analysis_tasks"})


def assert_empty_business_schema(connection) -> None:
    """Require explicit verification/baseline for a database with existing data."""
    existing = BUSINESS_TABLES.intersection(inspect(connection).get_table_names())
    if existing:
        names = ", ".join(sorted(existing))
        raise RuntimeError(
            f"existing business tables ({names}); verify Flyway schema and baseline explicitly"
        )


def split_sql_statements(sql: str) -> list[str]:
    """Split outside SQL quotes; retain doubled apostrophes in dynamic DDL."""
    statements: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == "\\" and index + 1 < len(sql):
                index += 2
                continue
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    if quote:
        raise ValueError("unterminated SQL quote in migration")
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def migration_statements(version: int) -> list[str]:
    try:
        path = SOURCE_DIR / SOURCE_NAMES[version]
    except KeyError as exc:
        raise ValueError(f"unknown pinned migration V{version}") from exc
    return split_sql_statements(path.read_text(encoding="utf-8"))


def execute_migration(connection, version: int) -> None:
    # V2/V3 SET variables and PREPARE/EXECUTE share this exact DBAPI connection.
    for statement in migration_statements(version):
        connection.exec_driver_sql(statement)
