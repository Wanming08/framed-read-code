"""ORM mapping of the existing V1–V3 MySQL tables.

Alembic executes the original SQL. These mappings never create schema on startup.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.mysql import LONGTEXT, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uk_users_username", "username", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="USER", server_default=text("'USER'"))


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        Index("idx_media_content_hash", "content_hash"),
        Index("idx_media_user_time", "user_id", "upload_time"),
        Index("idx_media_status_time", "status", "upload_time"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    upload_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(fsp=3), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AgentCheckpoint(Base):
    __tablename__ = "agent_checkpoints"
    __table_args__ = (
        PrimaryKeyConstraint("media_id", "checkpoint_key"),
        Index("idx_agent_checkpoint_updated", "updated_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_key: Mapped[str] = mapped_column(String(160), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(fsp=3), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=text("CURRENT_TIMESTAMP(3)"),
    )


class FailedAnalysisTask(Base):
    __tablename__ = "failed_analysis_tasks"
    __table_args__ = (
        Index("idx_failed_analysis_status_time", "status", "created_at"),
        Index("idx_failed_analysis_media", "media_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERAL", server_default=text("'GENERAL'"))
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_goal: Mapped[str] = mapped_column(String(500), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="FAILED", server_default=text("'FAILED'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(fsp=3), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(fsp=3), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)"),
        server_onupdate=text("CURRENT_TIMESTAMP(3)"),
    )
