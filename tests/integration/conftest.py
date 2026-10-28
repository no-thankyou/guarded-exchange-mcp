"""Real SQLite in a temporary directory (a file, not ``:memory:``: WAL and multiple connections)."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from guarded_exchange.config import Settings
from guarded_exchange.infrastructure.db.migrate import upgrade_to_head
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine

BUSY_TIMEOUT_MS = 5000


def make_settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "db_path": root / "data" / "gx.db",
        "master_key_file": root / "conf" / "master.key",
        "sqlite_busy_timeout_ms": BUSY_TIMEOUT_MS,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "gx.db"
    upgrade_to_head(path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    return path


@pytest.fixture
async def uow(db_path: Path) -> AsyncIterator[SqlUnitOfWorkFactory]:
    engine = create_sqlite_async_engine(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        yield SqlUnitOfWorkFactory(engine)
    finally:
        await engine.dispose()
