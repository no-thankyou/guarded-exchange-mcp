"""users table.

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("name", name="uq_users_name"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_users_status"),
        sa.CheckConstraint("environment IN ('testnet', 'mainnet')", name="ck_users_environment"),
    )


def downgrade() -> None:
    op.drop_table("users")
