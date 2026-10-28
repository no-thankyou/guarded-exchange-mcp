"""Table definitions used by repositories (SQLAlchemy Core, no ORM).

Migrations in ``migrations/versions`` are written by hand and frozen; this module describes the
current head and must stay in sync with them
(``tests/integration/test_sqlite_storage.py::test_schema_module_matches_the_migrations`` compares).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
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

# One row per configured user (FR-12). No row = default ceiling for the user's environment and no
# overrides; ``ceiling`` NULL = the admin never set one. Both JSON columns hold canonical JSON.
policies = Table(
    "policies",
    metadata,
    Column("user_id", String(64), ForeignKey("users.id"), primary_key=True),
    Column("ceiling", Text, nullable=True),
    Column("overrides", Text, nullable=False),
    Column("updated_at", EpochMillis(), nullable=False),
)

# Bearer tokens of the remote mode (ADR-004): only the SHA-256 hex of the secret, never the secret.
# Revocation sets ``revoked_at``; rows are kept for the journal's ``token_id`` references.
access_tokens = Table(
    "access_tokens",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), ForeignKey("users.id"), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", EpochMillis(), nullable=False),
    Column("revoked_at", EpochMillis(), nullable=True),
    CheckConstraint("length(token_hash) = 64", name="token_hash_sha256"),
)

# Exchange API keys (ticket 007, PLAN 5.2, D-9, D-12, ADR-009). ``ciphertext`` is AES-256-GCM of the
# key pair with AAD = credential_id || user_id || exchange; nothing else holds key material.
# ``permissions`` is the last answer of the exchange as canonical JSON (NULL: accepted on admin
# attestation, never verified). Soft delete: ``status = 'deleted'`` + ``deleted_at``.
exchange_credentials = Table(
    "exchange_credentials",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), ForeignKey("users.id"), nullable=False),
    Column("exchange", String(32), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("permissions", Text, nullable=True),
    Column("permissions_checked_at", EpochMillis(), nullable=True),
    Column("admin_attested", Boolean, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", EpochMillis(), nullable=False),
    Column("deleted_at", EpochMillis(), nullable=True),
    CheckConstraint("environment IN ('testnet', 'mainnet')", name="environment"),
    CheckConstraint("status IN ('active', 'deleted')", name="status"),
    CheckConstraint("length(nonce) = 12", name="nonce_96_bits"),
    CheckConstraint("key_version >= 1", name="key_version_positive"),
    CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name="deleted_at_iff_deleted"),
)
# D-12: at most one active key per user, exchange and environment (partial unique index).
Index(
    "uq_exchange_credentials_active",
    exchange_credentials.c.user_id,
    exchange_credentials.c.exchange,
    exchange_credentials.c.environment,
    unique=True,
    sqlite_where=text("status = 'active'"),
    postgresql_where=text("status = 'active'"),
)
