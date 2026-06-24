"""initial schema: messages + attachments

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

source_system = postgresql.ENUM("github", "telegram", "email", name="source_system")
processing_status = postgresql.ENUM("raw", "normalized", "error", name="processing_status")


def upgrade() -> None:
    bind = op.get_bind()
    source_system.create(bind, checkfirst=True)
    processing_status.create(bind, checkfirst=True)

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "source_system",
            postgresql.ENUM(name="source_system", create_type=False),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("thread_external_id", sa.String(length=255), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=1024), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processing_status",
            postgresql.ENUM(name="processing_status", create_type=False),
            nullable=False,
        ),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("source_system", "external_id", name="uq_messages_source_external"),
    )
    op.create_index("ix_messages_status", "messages", ["processing_status"])
    op.create_index(
        "ix_messages_source_created_at", "messages", ["source_system", "source_created_at"]
    )

    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("s3_bucket", sa.String(length=255), nullable=True),
        sa.Column("s3_key", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"],
            name="fk_attachments_message_id_messages", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attachments"),
        sa.UniqueConstraint("message_id", "external_id", name="uq_attachments_message_external"),
    )
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_attachments_message_id", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_messages_source_created_at", table_name="messages")
    op.drop_index("ix_messages_status", table_name="messages")
    op.drop_table("messages")

    bind = op.get_bind()
    processing_status.drop(bind, checkfirst=True)
    source_system.drop(bind, checkfirst=True)
