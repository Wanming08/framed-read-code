"""Alembic environment for the original MySQL Flyway SQL files."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url and not config.attributes.get("connection"):
        raise RuntimeError("DATABASE_URL is required to run database migrations")

    supplied_connection = config.attributes.get("connection")
    engine = None if supplied_connection is not None else create_engine(url, pool_pre_ping=True)
    try:
        if supplied_connection is not None:
            context.configure(connection=supplied_connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        else:
            with engine.connect() as connection:
                context.configure(connection=connection, target_metadata=target_metadata)
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        if engine is not None:
            engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline SQL generation is unsupported for prepared MySQL migrations")
run_migrations_online()
