"""exchange_credentials table: encrypted exchange API keys (ticket 007).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_credentials",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("permissions", sa.Text(), nullable=True),
        sa.Column("permissions_checked_at", sa.BigInteger(), nullable=True),
        sa.Column("admin_attested", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_exchange_credentials_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_exchange_credentials"),
        sa.CheckConstraint(
            "environment IN ('testnet', 'mainnet')", name="ck_exchange_credentials_environment"
        ),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_exchange_credentials_status"),
        sa.CheckConstraint("length(nonce) = 12", name="ck_exchange_credentials_nonce_96_bits"),
        sa.CheckConstraint("key_version >= 1", name="ck_exchange_credentials_key_version_positive"),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_exchange_credentials_deleted_at_iff_deleted",
        ),
    )
    op.create_index(
        "uq_exchange_credentials_active",
        "exchange_credentials",
        ["user_id", "exchange", "environment"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_exchange_credentials_active", table_name="exchange_credentials")
    op.drop_table("exchange_credentials")
