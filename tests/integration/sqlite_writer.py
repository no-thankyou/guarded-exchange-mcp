"""Worker for the two-process SQLite test: ``python -m tests.integration.sqlite_writer DB N GO``.

Waits for the ``GO`` file (so both workers start together), then runs ``N`` write units of
work that first read and then write - the pattern that fails with ``database is locked`` under
deferred transactions. Exit code 0 means every transaction committed.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from guarded_exchange.domain.audit import AuditEvent
from guarded_exchange.infrastructure.clock import SystemClock
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine

CHAIN = "race"


async def main(db_path: Path, count: int) -> None:
    engine = create_sqlite_async_engine(db_path, busy_timeout_ms=10_000)
    uow = SqlUnitOfWorkFactory(engine)
    clock = SystemClock()
    try:
        for i in range(count):
            async with uow(write=True) as tx:
                await tx.audit.list(CHAIN, limit=1)
                await asyncio.sleep(0.002)  # hold the transaction so the other process collides
                await tx.audit.append(CHAIN, AuditEvent("race", f"pid:{os.getpid()}", {"i": i}), clock.now())
    finally:
        await engine.dispose()


def wait_for(go: Path) -> None:
    while not go.exists():
        time.sleep(0.005)


if __name__ == "__main__":
    wait_for(Path(sys.argv[3]))
    asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2])))
