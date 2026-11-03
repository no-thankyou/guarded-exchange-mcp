"""SQL repositories: the only place with SQL for these tables. Rows map to domain objects here."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, and_, case, delete, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from guarded_exchange.domain.access_tokens import AccessToken
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, JsonValue, canonical_json
from guarded_exchange.domain.credentials import (
    Credential,
    CredentialStatus,
    KeyPermissions,
    SealedSecret,
    permissions_from_payload,
    permissions_to_payload,
)
from guarded_exchange.domain.idempotency import IN_FLIGHT_GUARD, IdempotencyRecord, KeyKind, RecordStatus
from guarded_exchange.domain.kill_switch import KillSwitch
from guarded_exchange.domain.policy import (
    PolicyRecord,
    ceiling_from_payload,
    patch_from_payload,
    patch_to_payload,
    settings_to_payload,
)
from guarded_exchange.domain.users import Environment, User, UserNameTaken, UserStatus
from guarded_exchange.infrastructure.db.dialect import EpochMillis, insert_for
from guarded_exchange.infrastructure.db.schema import (
    access_tokens,
    audit_heads,
    audit_log,
    exchange_credentials,
    idempotency_records,
    policies,
    users,
)


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
        try:
            await self._conn.execute(
                users.insert().values(
                    id=user.id,
                    name=user.name,
                    status=user.status.value,
                    environment=user.environment.value,
                    created_at=user.created_at,
                    kill_switch=user.kill_switch.value,
                )
            )
        except IntegrityError as exc:
            # SQLite names the column, Postgres the constraint; a duplicate id stays a raw error.
            if any(marker in str(exc.orig) for marker in ("users.name", "uq_users_name")):
                raise UserNameTaken(user.name) from exc
            raise

    async def get(self, user_id: str) -> User | None:
        row = (await self._conn.execute(select(users).where(users.c.id == user_id))).one_or_none()
        return None if row is None else _user_from_row(row)

    async def get_by_name(self, name: str) -> User | None:
        row = (await self._conn.execute(select(users).where(users.c.name == name))).one_or_none()
        return None if row is None else _user_from_row(row)

    async def list(self) -> Sequence[User]:
        return [_user_from_row(row) for row in await self._conn.execute(select(users).order_by(users.c.name))]

    async def set_kill_switch(self, user_id: str, state: KillSwitch) -> None:
        self._require_writable("users.set_kill_switch")
        await self._conn.execute(update(users).where(users.c.id == user_id).values(kill_switch=state.value))


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

    async def revoke(self, token_id: str, at: datetime) -> bool:
        self._require_writable("access_tokens.revoke")
        result = await self._conn.execute(
            update(access_tokens)
            .where(access_tokens.c.id == token_id, access_tokens.c.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        return result.rowcount == 1


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

    async def since(self, chain_id: str, at: datetime, event_types: frozenset[str]) -> Sequence[AuditRecord]:
        query = (
            select(audit_log)
            .where(
                audit_log.c.chain_id == chain_id,
                audit_log.c.ts >= at,
                audit_log.c.event_type.in_(sorted(event_types)),
            )
            .order_by(audit_log.c.seq)
        )
        return [_audit_record(row) for row in await self._conn.execute(query)]

    async def list(self, chain_id: str, *, after_seq: int = 0, limit: int = 100) -> Sequence[AuditRecord]:
        query = (
            select(audit_log)
            .where(audit_log.c.chain_id == chain_id, audit_log.c.seq > after_seq)
            .order_by(audit_log.c.seq)
            .limit(limit)
        )
        return [_audit_record(row) for row in await self._conn.execute(query)]


def _audit_record(row: Any) -> AuditRecord:
    return AuditRecord(
        chain_id=row.chain_id,
        seq=row.seq,
        ts=row.ts,
        event_type=row.event_type,
        actor=row.actor,
        payload=json.loads(row.payload),
    )


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


class SqlCredentialRepository(_Repository):
    """Exchange keys; ``ciphertext``/``nonce`` leave this class only as ``SealedSecret``."""

    async def add(self, credential: Credential, sealed: SealedSecret) -> None:
        self._require_writable("credentials.add")
        await self._conn.execute(
            exchange_credentials.insert().values(
                id=credential.id,
                user_id=credential.user_id,
                exchange=credential.exchange,
                environment=credential.environment.value,
                ciphertext=sealed.ciphertext,
                nonce=sealed.nonce,
                key_version=sealed.key_version,
                permissions=_permissions_json(credential.permissions),
                permissions_checked_at=credential.permissions_checked_at,
                admin_attested=credential.admin_attested,
                status=credential.status.value,
                created_at=credential.created_at,
                deleted_at=credential.deleted_at,
            )
        )

    async def get(self, credential_id: str) -> Credential | None:
        query = select(*_CREDENTIAL_COLUMNS).where(exchange_credentials.c.id == credential_id)
        row = (await self._conn.execute(query)).one_or_none()
        return None if row is None else _credential_from_row(row)

    async def get_active(self, user_id: str, exchange: str, environment: Environment) -> Credential | None:
        query = select(*_CREDENTIAL_COLUMNS).where(
            exchange_credentials.c.user_id == user_id,
            exchange_credentials.c.exchange == exchange,
            exchange_credentials.c.environment == environment.value,
            exchange_credentials.c.status == CredentialStatus.ACTIVE.value,
        )
        row = (await self._conn.execute(query)).one_or_none()
        return None if row is None else _credential_from_row(row)

    async def list_for_user(self, user_id: str) -> Sequence[Credential]:
        query = (
            select(*_CREDENTIAL_COLUMNS)
            .where(exchange_credentials.c.user_id == user_id)
            .order_by(exchange_credentials.c.created_at, exchange_credentials.c.id)
        )
        return [_credential_from_row(row) for row in await self._conn.execute(query)]

    async def list_active(self, exchange: str, environment: Environment) -> Sequence[Credential]:
        query = (
            select(*_CREDENTIAL_COLUMNS)
            .where(
                exchange_credentials.c.exchange == exchange,
                exchange_credentials.c.environment == environment.value,
                exchange_credentials.c.status == CredentialStatus.ACTIVE.value,
            )
            .order_by(exchange_credentials.c.created_at, exchange_credentials.c.id)
        )
        return [_credential_from_row(row) for row in await self._conn.execute(query)]

    async def get_sealed(self, credential_id: str) -> SealedSecret | None:
        return await self._sealed(exchange_credentials.c.id == credential_id)

    async def get_sealed_active(self, credential_id: str) -> SealedSecret | None:
        return await self._sealed(
            exchange_credentials.c.id == credential_id,
            exchange_credentials.c.status == CredentialStatus.ACTIVE.value,
        )

    async def _sealed(self, *where: ColumnElement[bool]) -> SealedSecret | None:
        query = select(
            exchange_credentials.c.ciphertext,
            exchange_credentials.c.nonce,
            exchange_credentials.c.key_version,
        ).where(*where)
        row = (await self._conn.execute(query)).one_or_none()
        if row is None:
            return None
        return SealedSecret(
            ciphertext=bytes(row.ciphertext), nonce=bytes(row.nonce), key_version=row.key_version
        )

    async def update_permissions(
        self, credential_id: str, permissions: KeyPermissions, checked_at: datetime
    ) -> None:
        self._require_writable("credentials.update_permissions")
        await self._conn.execute(
            update(exchange_credentials)
            .where(exchange_credentials.c.id == credential_id)
            .values(permissions=_permissions_json(permissions), permissions_checked_at=checked_at)
        )

    async def mark_deleted(self, credential_id: str, at: datetime) -> bool:
        self._require_writable("credentials.mark_deleted")
        result = await self._conn.execute(
            update(exchange_credentials)
            .where(
                exchange_credentials.c.id == credential_id,
                exchange_credentials.c.status == CredentialStatus.ACTIVE.value,
            )
            .values(status=CredentialStatus.DELETED.value, deleted_at=at)
        )
        return result.rowcount == 1


class SqlIdempotencyRepository(_Repository):
    """Claims of state-changing calls (ADR-014). Every change is conditional on the holder's
    ``action_id``, so a call whose record was taken over cannot touch the new holder's row."""

    async def claim(self, record: IdempotencyRecord, now: datetime) -> IdempotencyRecord:
        self._require_writable("idempotency.claim")
        t = idempotency_records
        values = {
            "user_id": record.user_id,
            "key_kind": record.kind.value,
            "key": record.key,
            "request_hash": record.request_hash,
            "action_id": record.action_id,
            "status": record.status.value,
            "result": None,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "sent_at": None,
        }
        insert = insert_for(self._conn, t).values(**values)
        # One statement: insert, or take over the key only if its record has expired (ADR-014).
        upsert = insert.on_conflict_do_update(
            index_elements=[t.c.user_id, t.c.key_kind, t.c.key],
            set_={
                name: insert.excluded[name] for name in values if name not in {"user_id", "key_kind", "key"}
            },
            where=or_(
                t.c.expires_at <= now,
                and_(
                    t.c.status == RecordStatus.IN_PROGRESS.value,
                    t.c.created_at <= now - IN_FLIGHT_GUARD,  # stuck before sending: fenced anyway
                ),
            ),
        ).returning(t.c.action_id)
        claimed = (await self._conn.execute(upsert)).scalar_one_or_none()
        await self._conn.execute(  # housekeeping: expired answered records of this user only; an
            delete(t).where(  # unanswered one may still have a holder in flight (fenced by expiry)
                t.c.user_id == record.user_id,
                t.c.expires_at <= now,
                t.c.status.in_([RecordStatus.UNKNOWN.value, RecordStatus.COMPLETED.value]),
            )
        )
        if claimed == record.action_id:
            return record
        row = (await self._conn.execute(select(t).where(*self._key(record)))).one()
        return _idempotency_from_row(row)

    async def mark_sent(self, record: IdempotencyRecord, now: datetime) -> bool:
        self._require_writable("idempotency.mark_sent")
        t = idempotency_records
        guard = literal(now + IN_FLIGHT_GUARD, EpochMillis())
        result = await self._conn.execute(
            update(t)
            .where(*self._held(record), t.c.status == RecordStatus.IN_PROGRESS.value, t.c.expires_at > now)
            .values(
                status=RecordStatus.SENT.value,
                sent_at=now,
                expires_at=case((t.c.expires_at > guard, t.c.expires_at), else_=guard),
            )
        )
        return result.rowcount == 1

    async def current(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        row = (await self._conn.execute(select(idempotency_records).where(*self._key(record)))).one_or_none()
        return None if row is None else _idempotency_from_row(row)

    async def finish(
        self,
        record: IdempotencyRecord,
        status: RecordStatus,
        result: Mapping[str, JsonValue],
        expires_at: datetime,
    ) -> bool:
        self._require_writable("idempotency.finish")
        if status not in {RecordStatus.UNKNOWN, RecordStatus.COMPLETED}:
            raise ValueError(f"{status} is not an answered status")
        changed = await self._conn.execute(
            update(idempotency_records)
            .where(*self._held(record))
            .values(status=status.value, result=canonical_json(result), expires_at=expires_at)
        )
        return changed.rowcount == 1

    async def release(self, record: IdempotencyRecord) -> bool:
        self._require_writable("idempotency.release")
        result = await self._conn.execute(delete(idempotency_records).where(*self._held(record)))
        return result.rowcount == 1

    @staticmethod
    def _key(record: IdempotencyRecord) -> tuple[ColumnElement[bool], ...]:
        t = idempotency_records
        return (t.c.user_id == record.user_id, t.c.key_kind == record.kind.value, t.c.key == record.key)

    @classmethod
    def _held(cls, record: IdempotencyRecord) -> tuple[ColumnElement[bool], ...]:
        return (*cls._key(record), idempotency_records.c.action_id == record.action_id)


def _idempotency_from_row(row: Row[tuple[object, ...]]) -> IdempotencyRecord:
    mapping = row._mapping
    raw_result = mapping["result"]
    return IdempotencyRecord(
        user_id=str(mapping["user_id"]),
        kind=KeyKind(mapping["key_kind"]),
        key=str(mapping["key"]),
        request_hash=str(mapping["request_hash"]),
        action_id=str(mapping["action_id"]),
        status=RecordStatus(mapping["status"]),
        created_at=mapping["created_at"],
        expires_at=mapping["expires_at"],
        result=None if raw_result is None else json.loads(str(raw_result)),
        sent_at=mapping["sent_at"],
    )


# Every column except the encrypted secret: listing keys never loads ciphertext.
_CREDENTIAL_COLUMNS = tuple(
    column for column in exchange_credentials.c if column.name not in {"ciphertext", "nonce", "key_version"}
)


def _permissions_json(permissions: KeyPermissions | None) -> str | None:
    payload = permissions_to_payload(permissions)
    return None if payload is None else canonical_json(payload)


def _credential_from_row(row: Row[tuple[object, ...]]) -> Credential:
    mapping = row._mapping
    raw_permissions = mapping["permissions"]
    return Credential(
        id=str(mapping["id"]),
        user_id=str(mapping["user_id"]),
        exchange=str(mapping["exchange"]),
        environment=Environment(mapping["environment"]),
        status=CredentialStatus(mapping["status"]),
        permissions=None
        if raw_permissions is None
        else permissions_from_payload(json.loads(str(raw_permissions))),
        permissions_checked_at=mapping["permissions_checked_at"],
        admin_attested=bool(mapping["admin_attested"]),
        created_at=mapping["created_at"],
        deleted_at=mapping["deleted_at"],
    )


def _user_from_row(row: Row[tuple[object, ...]]) -> User:
    mapping = row._mapping
    return User(
        id=str(mapping["id"]),
        name=str(mapping["name"]),
        status=UserStatus(mapping["status"]),
        environment=Environment(mapping["environment"]),
        created_at=mapping["created_at"],
        kill_switch=KillSwitch(mapping["kill_switch"]),
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
