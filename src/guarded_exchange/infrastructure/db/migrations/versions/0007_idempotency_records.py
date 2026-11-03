"""idempotency_records table: claims of state-changing calls (ticket 012).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("key_kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_idempotency_records_user_id_users"),
        sa.PrimaryKeyConstraint("user_id", "key_kind", "key", name="pk_idempotency_records"),
        sa.CheckConstraint("key_kind IN ('explicit', 'fingerprint')", name="ck_idempotency_records_key_kind"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'sent', 'unknown', 'completed')", name="ck_idempotency_records_status"
        ),
        sa.CheckConstraint(
            "(result IS NULL) = (status IN ('in_progress', 'sent'))",
            name="ck_idempotency_records_result_iff_answered",
        ),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_idempotency_records_request_hash_sha256"),
    )
    op.create_index(
        "ix_idempotency_records_user_id_expires_at", "idempotency_records", ["user_id", "expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_user_id_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
