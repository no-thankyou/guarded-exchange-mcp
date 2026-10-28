"""policies table: admin ceiling and user overrides per user (ticket 006).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("ceiling", sa.Text(), nullable=True),
        sa.Column("overrides", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_policies_user_id_users"),
        sa.PrimaryKeyConstraint("user_id", name="pk_policies"),
    )


def downgrade() -> None:
    op.drop_table("policies")
