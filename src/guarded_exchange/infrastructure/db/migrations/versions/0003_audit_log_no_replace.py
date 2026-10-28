"""audit_log: forbid INSERT OR REPLACE / REPLACE INTO over an existing record.

REPLACE resolves the primary-key conflict by deleting the old row, and that delete does not
fire DELETE triggers unless ``recursive_triggers`` is on in the writing connection - which an
attacker's connection would not set. This trigger aborts any insert of an existing
(chain_id, seq), whatever the conflict clause.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
"""

from alembic import op

revision = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER audit_log_no_replace BEFORE INSERT ON audit_log "
        "WHEN EXISTS (SELECT 1 FROM audit_log WHERE chain_id = NEW.chain_id AND seq = NEW.seq) "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_replace")
