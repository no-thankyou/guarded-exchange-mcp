"""Table definitions used by repositories (SQLAlchemy Core, no ORM).

Migrations in ``migrations/versions`` are written by hand and frozen; this module describes the
current head and must stay in sync with them (``tests/integration/test_storage.py`` compares).
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from guarded_exchange.infrastructure.db.dialect import EpochMillis

metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
    }
)

users = Table(
    "users",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("status", String(16), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("created_at", EpochMillis(), nullable=False),
    CheckConstraint("status IN ('active', 'revoked')", name="status"),
    CheckConstraint("environment IN ('testnet', 'mainnet')", name="environment"),
)

# Append-only (FR-37): BEFORE UPDATE/DELETE triggers raise. No MAC until ticket 024 (D-13).
audit_log = Table(
    "audit_log",
    metadata,
    Column("chain_id", String(64), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("ts", EpochMillis(), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("payload", Text, nullable=False),  # canonical JSON
    PrimaryKeyConstraint("chain_id", "seq"),
    CheckConstraint("seq >= 1", name="seq_positive"),
)

# One row per chain: the last used seq. Bumping it serialises appends within a chain.
audit_heads = Table(
    "audit_heads",
    metadata,
    Column("chain_id", String(64), primary_key=True),
    Column("last_seq", Integer, nullable=False),
)
