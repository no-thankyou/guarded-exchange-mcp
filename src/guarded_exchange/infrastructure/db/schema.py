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
    # FR-16 (migration 0008): who turned the kill switch on, or 'none'.
    Column("kill_switch", String(16), nullable=False, server_default="none"),
    CheckConstraint("status IN ('active', 'revoked')", name="status"),
    CheckConstraint("environment IN ('testnet', 'mainnet')", name="environment"),
    CheckConstraint("kill_switch IN ('none', 'user', 'admin')", name="kill_switch"),
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

# Claims of state-changing calls (FR-26, ADR-014, D-20; ticket 012): one row per user, key kind and
# key. ``action_id`` is the call holding the row (its client order id is derived from it, D-19);
# ``result`` is the stored answer as canonical JSON, present exactly once the call has one.
idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("user_id", String(64), ForeignKey("users.id"), nullable=False),
    Column("key_kind", String(16), nullable=False),
    Column("key", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("action_id", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("result", Text, nullable=True),
    Column("created_at", EpochMillis(), nullable=False),
    Column("expires_at", EpochMillis(), nullable=False),
    Column("sent_at", EpochMillis(), nullable=True),
    PrimaryKeyConstraint("user_id", "key_kind", "key"),
    CheckConstraint("key_kind IN ('explicit', 'fingerprint')", name="key_kind"),
    CheckConstraint("status IN ('in_progress', 'sent', 'unknown', 'completed')", name="status"),
    CheckConstraint("(result IS NULL) = (status IN ('in_progress', 'sent'))", name="result_iff_answered"),
    CheckConstraint("length(request_hash) = 64", name="request_hash_sha256"),
)
Index(
    "ix_idempotency_records_user_id_expires_at",
    idempotency_records.c.user_id,
    idempotency_records.c.expires_at,
)

# Pending confirmations (tickets 016/017, PLAN ADR-005/006, 5.2): an action parked until a human decides.
# ``id`` doubles as the action id (the client order id is derived from it). ``action``, ``shown`` and
# ``result`` are canonical JSON; the two digests let the executor detect a changed row (FR-22).
pending_confirmations = Table(
    "pending_confirmations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), ForeignKey("users.id"), nullable=False),
    Column("credential_id", String(64), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("tool", String(32), nullable=False),
    Column("action", Text, nullable=False),
    Column("action_digest", String(64), nullable=False),
    Column("terms_digest", String(64), nullable=False),
    Column("risk", String(16), nullable=False),
    Column("shown", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("idempotency_key", String(64), nullable=True),
    Column("created_at", EpochMillis(), nullable=False),
    Column("expires_at", EpochMillis(), nullable=False),
    Column("approved_at", EpochMillis(), nullable=True),
    Column("decided_at", EpochMillis(), nullable=True),
    Column("decided_by", String(128), nullable=True),
    Column("channel", String(16), nullable=True),
    Column("exec_started_at", EpochMillis(), nullable=True),
    Column("result", Text, nullable=True),
    CheckConstraint("environment IN ('testnet', 'mainnet')", name="environment"),
    CheckConstraint("risk IN ('increases', 'reduces')", name="risk"),
    CheckConstraint(
        "status IN ('pending', 'approved', 'executing', 'executed', 'failed', 'unknown', 'rejected', "
        "'expired', 'expired_after_approval', 'cancelled')",
        name="status",
    ),
    CheckConstraint("length(action_digest) = 64 AND length(terms_digest) = 64", name="digest_sha256"),
)
Index(
    "ix_pending_confirmations_user_id_status", pending_confirmations.c.user_id, pending_confirmations.c.status
)
Index(
    "ix_pending_confirmations_status_expires_at",
    pending_confirmations.c.status,
    pending_confirmations.c.expires_at,
)
Index(
    "ix_pending_confirmations_user_id_action_digest",
    pending_confirmations.c.user_id,
    pending_confirmations.c.action_digest,
)
Index(
    "ix_pending_confirmations_user_id_idempotency_key",
    pending_confirmations.c.user_id,
    pending_confirmations.c.idempotency_key,
)
