"""audit_log (append-only, triggers) and audit_heads.

Any later batch migration that recreates ``audit_log`` must recreate these triggers too
(PLAN D-11); the server refuses to start without them.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("chain_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("chain_id", "seq", name="pk_audit_log"),
        sa.CheckConstraint("seq >= 1", name="ck_audit_log_seq_positive"),
    )
    op.create_table(
        "audit_heads",
        sa.Column("chain_id", sa.String(64), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("chain_id", name="pk_audit_heads"),
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_delete")
    op.execute("DROP TRIGGER audit_log_no_update")
    op.drop_table("audit_heads")
    op.drop_table("audit_log")
