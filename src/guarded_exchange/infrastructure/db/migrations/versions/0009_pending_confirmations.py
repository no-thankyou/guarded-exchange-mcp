"""pending_confirmations table: actions parked until a human decides (tickets 016, 017).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

_STATUSES = (
    "'pending', 'approved', 'executing', 'executed', 'failed', 'unknown', 'rejected', "
    "'expired', 'expired_after_approval', 'cancelled'"
)


def upgrade() -> None:
    op.create_table(
        "pending_confirmations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("terms_digest", sa.String(64), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("shown", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("approved_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("exec_started_at", sa.BigInteger(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_pending_confirmations_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_pending_confirmations"),
        sa.CheckConstraint(
            "environment IN ('testnet', 'mainnet')", name="ck_pending_confirmations_environment"
        ),
        sa.CheckConstraint("risk IN ('increases', 'reduces')", name="ck_pending_confirmations_risk"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_pending_confirmations_status"),
        sa.CheckConstraint(
            "length(action_digest) = 64 AND length(terms_digest) = 64",
            name="ck_pending_confirmations_digest_sha256",
        ),
    )
    op.create_index("ix_pending_confirmations_user_id_status", "pending_confirmations", ["user_id", "status"])
    op.create_index(
        "ix_pending_confirmations_status_expires_at", "pending_confirmations", ["status", "expires_at"]
    )
    op.create_index(
        "ix_pending_confirmations_user_id_action_digest",
        "pending_confirmations",
        ["user_id", "action_digest"],
    )
    op.create_index(
        "ix_pending_confirmations_user_id_idempotency_key",
        "pending_confirmations",
        ["user_id", "idempotency_key"],
    )


def downgrade() -> None:
    for name in (
        "ix_pending_confirmations_user_id_idempotency_key",
        "ix_pending_confirmations_user_id_action_digest",
        "ix_pending_confirmations_status_expires_at",
        "ix_pending_confirmations_user_id_status",
    ):
        op.drop_index(name, table_name="pending_confirmations")
    op.drop_table("pending_confirmations")
