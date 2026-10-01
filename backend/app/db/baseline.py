"""Explicit, guarded adoption of an existing Flyway V1–V3 MySQL database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db.migration_sql import BUSINESS_TABLES, SOURCE_NAMES


def _columns(*values: tuple[str, str, bool, str | None, str | None]):
    return {name: (column_type, nullable, default, extra) for name, column_type, nullable, default, extra in values}


# Independent transcription of pinned V1 plus V2/V3 final shape. The model is
# deliberately not used as the oracle for adopting an existing database.
EXPECTED_COLUMNS = {
    "users": _columns(
        ("id", "bigint", False, None, "auto_increment"),
        ("username", "varchar(32)", False, None, None),
        ("password", "varchar(255)", False, None, None),
        ("nickname", "varchar(50)", False, None, None),
        ("avatar", "varchar(512)", True, None, None),
        ("role", "varchar(32)", False, "USER", None),
    ),
    "media_files": _columns(
        ("id", "bigint", False, None, "auto_increment"),
        ("user_id", "bigint", False, None, None),
        ("filename", "varchar(255)", False, None, None),
        ("status", "varchar(32)", False, None, None),
        ("file_path", "varchar(1024)", False, None, None),
        ("content_hash", "varchar(64)", True, None, None),
        ("ai_summary", "longtext", True, None, None),
        ("transcript_text", "longtext", True, None, None),
        ("cover_url", "varchar(1024)", True, None, None),
        ("upload_time", "timestamp(3)", False, "CURRENT_TIMESTAMP(3)", None),
    ),
    "agent_checkpoints": _columns(
        ("media_id", "bigint", False, None, None),
        ("checkpoint_key", "varchar(160)", False, None, None),
        ("stage", "varchar(64)", False, None, None),
        ("payload", "longtext", True, None, None),
        ("updated_at", "timestamp(3)", False, "CURRENT_TIMESTAMP(3)", "on update CURRENT_TIMESTAMP(3)"),
    ),
    "failed_analysis_tasks": _columns(
        ("id", "bigint", False, None, "auto_increment"),
        ("media_id", "bigint", False, None, None),
        ("action", "varchar(32)", False, None, None),
        ("mode", "varchar(32)", False, "GENERAL", None),
        ("content_hash", "varchar(128)", False, None, None),
        ("user_goal", "varchar(500)", False, None, None),
        ("attempt_count", "int", False, None, None),
        ("error_type", "varchar(128)", False, None, None),
        ("error_message", "varchar(1000)", True, None, None),
        ("status", "varchar(32)", False, "FAILED", None),
        ("created_at", "timestamp(3)", False, "CURRENT_TIMESTAMP(3)", None),
        ("updated_at", "timestamp(3)", False, "CURRENT_TIMESTAMP(3)", "on update CURRENT_TIMESTAMP(3)"),
    ),
}

EXPECTED_INDEXES = {
    "users": {"PRIMARY": (True, ("id",)), "uk_users_username": (True, ("username",))},
    "media_files": {
        "PRIMARY": (True, ("id",)),
        "idx_media_content_hash": (False, ("content_hash",)),
        "idx_media_user_time": (False, ("user_id", "upload_time")),
        "idx_media_status_time": (False, ("status", "upload_time")),
    },
    "agent_checkpoints": {
        "PRIMARY": (True, ("media_id", "checkpoint_key")),
        "idx_agent_checkpoint_updated": (False, ("updated_at",)),
    },
    "failed_analysis_tasks": {
        "PRIMARY": (True, ("id",)),
        "idx_failed_analysis_status_time": (False, ("status", "created_at")),
        "idx_failed_analysis_media": (False, ("media_id",)),
    },
}


def verify_v3_schema(connection) -> None:
    """Reject any final-shape discrepancy before Alembic takes ownership."""
    missing = sorted(BUSINESS_TABLES - set(inspect(connection).get_table_names()))
    if missing:
        raise RuntimeError(f"missing original business tables: {', '.join(missing)}")
    if connection.dialect.name != "mysql":
        raise RuntimeError("Flyway baseline verification requires MySQL")
    rows = list(connection.exec_driver_sql(
        "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"
    ))
    tables = {name: (engine, collation) for name, engine, collation in rows}
    for table in sorted(BUSINESS_TABLES):
        if tables[table] != ("InnoDB", "utf8mb4_unicode_ci"):
            raise RuntimeError(f"{table} engine/collation does not match original V1")

    rows = connection.exec_driver_sql(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
    )
    actual_columns = {table: {} for table in BUSINESS_TABLES}
    for table, name, column_type, nullable, default, extra in rows:
        if table in actual_columns:
            normalized_extra = None
            if "auto_increment" in extra.lower():
                normalized_extra = "auto_increment"
            elif "on update CURRENT_TIMESTAMP(3)" in extra:
                normalized_extra = "on update CURRENT_TIMESTAMP(3)"
            actual_columns[table][name] = (column_type.lower(), nullable == "YES", default, normalized_extra)
    for table, expected in EXPECTED_COLUMNS.items():
        if actual_columns[table] != expected:
            raise RuntimeError(f"{table} columns differ from original V1–V3 schema")

    rows = connection.exec_driver_sql(
        "SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
    )
    actual_indexes = {table: {} for table in BUSINESS_TABLES}
    for table, name, _seq, column, non_unique in rows:
        if table in actual_indexes:
            entry = actual_indexes[table].setdefault(name, [not bool(non_unique), []])
            entry[1].append(column)
    actual_indexes = {
        table: {name: (unique, tuple(columns)) for name, (unique, columns) in entries.items()}
        for table, entries in actual_indexes.items()
    }
    for table, expected in EXPECTED_INDEXES.items():
        if actual_indexes[table] != expected:
            raise RuntimeError(f"{table} indexes differ from original V1–V3 schema")


def verify_flyway_history(connection) -> None:
    names = {row[0] for row in connection.exec_driver_sql(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
    )}
    if "flyway_schema_history" not in names:
        raise RuntimeError("Flyway history is missing; cannot claim an existing Flyway baseline")
    rows = list(connection.exec_driver_sql(
        "SELECT version, script, success FROM flyway_schema_history "
        "WHERE version IS NOT NULL ORDER BY installed_rank"
    ))
    expected = [(str(version), SOURCE_NAMES[version], 1) for version in (1, 2, 3)]
    if [(str(version), script, int(success)) for version, script, success in rows] != expected:
        raise RuntimeError("Flyway history does not contain exactly successful original V1–V3")


def baseline_existing_flyway_database(database_url: str, *, backup_confirmed: bool) -> None:
    """Stamp only after the operator has backed up and both checks have passed."""
    if not backup_confirmed:
        raise RuntimeError("backup confirmation is required before adopting an existing database")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            existing_tables = set(inspect(connection).get_table_names())
            if "alembic_version" in existing_tables:
                raise RuntimeError("Alembic version already exists; baseline must not run twice")
            verify_v3_schema(connection)
            verify_flyway_history(connection)
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.stamp(config, "head")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Flyway V1–V3 and register Alembic head")
    parser.add_argument("--backup-confirmed", action="store_true")
    arguments = parser.parse_args()
    url = os.getenv("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL must be set")
    baseline_existing_flyway_database(url, backup_confirmed=arguments.backup_confirmed)


if __name__ == "__main__":
    main()
