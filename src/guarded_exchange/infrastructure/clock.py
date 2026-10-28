"""System implementations of the ``Clock`` and ``IdGenerator`` ports."""

import uuid
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        # Truncated to milliseconds: that is the storage precision (PLAN D-10), so a value
        # read back from the database equals the one that was written.
        current = datetime.now(UTC)
        return current.replace(microsecond=current.microsecond - current.microsecond % 1000)


class UuidGenerator:
    def new_id(self) -> str:
        return uuid.uuid4().hex
