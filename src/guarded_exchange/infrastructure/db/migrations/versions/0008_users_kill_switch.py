"""users.kill_switch: the user's kill switch, off or on and by whom (ticket 015, FR-16).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # A column-level CHECK: SQLite cannot add a table constraint without rebuilding the table, and
    # rebuilding ``users`` would fight the foreign keys that point at it.
    op.add_column(
        "users",
        sa.Column(
            "kill_switch",
            sa.String(16),
            sa.CheckConstraint("kill_switch IN ('none', 'user', 'admin')", name="ck_users_kill_switch"),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "kill_switch")
