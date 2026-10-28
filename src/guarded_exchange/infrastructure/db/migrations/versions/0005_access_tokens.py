"""access_tokens table: bearer tokens of the remote mode, stored as SHA-256 hashes (ticket 026).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "access_tokens",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_access_tokens_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_access_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_access_tokens_token_hash"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_access_tokens_token_hash_sha256"),
    )


def downgrade() -> None:
    op.drop_table("access_tokens")
