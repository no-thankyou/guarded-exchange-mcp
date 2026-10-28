"""SQL repositories: the only place with SQL for these tables. Rows map to domain objects here."""

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from guarded_exchange.domain.access_tokens import AccessToken
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, canonical_json
from guarded_exchange.domain.policy import (
    PolicyRecord,
    ceiling_from_payload,
    patch_from_payload,
    patch_to_payload,
    settings_to_payload,
)
from guarded_exchange.domain.users import Environment, User, UserStatus
from guarded_exchange.infrastructure.db.dialect import insert_for
from guarded_exchange.infrastructure.db.schema import access_tokens, audit_heads, audit_log, policies, users


class ReadOnlyUnitOfWorkError(RuntimeError):
    """A write was attempted through a read unit of work (it would not hold the write lock)."""


class _Repository:
    def __init__(self, connection: AsyncConnection, *, writable: bool) -> None:
        self._conn = connection
        self._writable = writable

    def _require_writable(self, operation: str) -> None:
        if not self._writable:
            raise ReadOnlyUnitOfWorkError(f"{operation} needs a write unit of work")


class SqlUserRepository(_Repository):
    async def add(self, user: User) -> None:
        self._require_writable("users.add")
        await self._conn.execute(
            users.insert().values(
                id=user.id,
                name=user.name,
                status=user.status.value,
                environment=user.environment.value,
                created_at=user.created_at,
            )
        )

    async def get(self, user_id: str) -> User | None:
        row = (await self._conn.execute(select(users).where(users.c.id == user_id))).one_or_none()
        return None if row is None else _user_from_row(row)

    async def get_by_name(self, name: str) -> User | None:
        row = (await self._conn.execute(select(users).where(users.c.name == name))).one_or_none()
        return None if row is None else _user_from_row(row)

    async def list(self) -> Sequence[User]:
        return [_user_from_row(row) for row in await self._conn.execute(select(users).order_by(users.c.name))]


class SqlAccessTokenRepository(_Repository):
    """Bearer tokens by SHA-256 hash; the secret never reaches this layer."""

    async def add(self, token: AccessToken) -> None:
        self._require_writable("access_tokens.add")
        await self._conn.execute(
            access_tokens.insert().values(
                id=token.id,
                user_id=token.user_id,
                token_hash=token.token_hash,
                created_at=token.created_at,
                revoked_at=token.revoked_at,
            )
        )

    async def get(self, token_id: str) -> AccessToken | None:
        query = select(access_tokens).where(access_tokens.c.id == token_id)
        row = (await self._conn.execute(query)).one_or_none()
        return None if row is None else _token_from_row(row)

    async def get_by_hash(self, token_hash: str) -> AccessToken | None:
        query = select(access_tokens).where(access_tokens.c.token_hash == token_hash)
        row = (await self._conn.execute(query)).one_or_none()
        return None if row is None else _token_from_row(row)

    async def revoke(self, token_id: str, at: datetime) -> None:
        self._require_writable("access_tokens.revoke")
        await self._conn.execute(
            update(access_tokens)
            .where(access_tokens.c.id == token_id, access_tokens.c.revoked_at.is_(None))
            .values(revoked_at=at)
        )


class SqlAuditLogRepository(_Repository):
    """Append and read only. ``seq`` comes from one atomic upsert on ``audit_heads``."""

    async def append(self, chain_id: str, event: AuditEvent, at: datetime) -> AuditRecord:
        self._require_writable("audit.append")
        payload = canonical_json(event.payload)
        insert = insert_for(self._conn, audit_heads).values(chain_id=chain_id, last_seq=1)
        bump = insert.on_conflict_do_update(
            index_elements=[audit_heads.c.chain_id],
            set_={"last_seq": audit_heads.c.last_seq + 1},
        ).returning(audit_heads.c.last_seq)
        seq = (await self._conn.execute(bump)).scalar_one()
        await self._conn.execute(
            audit_log.insert().values(
                chain_id=chain_id,
                seq=seq,
                ts=at,
                event_type=str(event.event_type),
                actor=event.actor,
                payload=payload,
            )
        )
        return AuditRecord(
            chain_id=chain_id,
            seq=seq,
            ts=at,
            event_type=str(event.event_type),
            actor=event.actor,
            payload=json.loads(payload),
        )

    async def list(self, chain_id: str, *, after_seq: int = 0, limit: int = 100) -> Sequence[AuditRecord]:
        query = (
            select(audit_log)
            .where(audit_log.c.chain_id == chain_id, audit_log.c.seq > after_seq)
            .order_by(audit_log.c.seq)
            .limit(limit)
        )
        return [
            AuditRecord(
                chain_id=row.chain_id,
                seq=row.seq,
                ts=row.ts,
                event_type=row.event_type,
                actor=row.actor,
                payload=json.loads(row.payload),
            )
            for row in await self._conn.execute(query)
        ]


class SqlPolicyRepository(_Repository):
    """Policies as canonical JSON (decimals as strings, no floats); one upserted row per user."""

    async def get(self, user_id: str) -> PolicyRecord | None:
        row = (await self._conn.execute(select(policies).where(policies.c.user_id == user_id))).one_or_none()
        if row is None:
            return None
        return PolicyRecord(
            ceiling=None if row.ceiling is None else ceiling_from_payload(json.loads(row.ceiling)),
            overrides=patch_from_payload(json.loads(row.overrides)),
            updated_at=row.updated_at,
        )

    async def save(self, user_id: str, record: PolicyRecord) -> None:
        self._require_writable("policies.save")
        values = {
            "ceiling": None
            if record.ceiling is None
            else canonical_json(settings_to_payload(record.ceiling)),
            "overrides": canonical_json(patch_to_payload(record.overrides)),
            "updated_at": record.updated_at,
        }
        upsert = insert_for(self._conn, policies).values(user_id=user_id, **values)
        await self._conn.execute(
            upsert.on_conflict_do_update(index_elements=[policies.c.user_id], set_=values)
        )


def _user_from_row(row: Row[tuple[object, ...]]) -> User:
    mapping = row._mapping
    return User(
        id=str(mapping["id"]),
        name=str(mapping["name"]),
        status=UserStatus(mapping["status"]),
        environment=Environment(mapping["environment"]),
        created_at=mapping["created_at"],
    )


def _token_from_row(row: Row[tuple[object, ...]]) -> AccessToken:
    mapping = row._mapping
    return AccessToken(
        id=str(mapping["id"]),
        user_id=str(mapping["user_id"]),
        token_hash=str(mapping["token_hash"]),
        created_at=mapping["created_at"],
        revoked_at=mapping["revoked_at"],
    )
